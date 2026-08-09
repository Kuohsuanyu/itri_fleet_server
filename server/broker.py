"""Embedded MQTT 3.1.1 broker (pure asyncio, no external dependency).

Implements the subset a telemetry fleet actually needs:
CONNECT/CONNACK, PUBLISH QoS 0+1, SUBSCRIBE/UNSUBSCRIBE, retained messages,
last-will-and-testament, keepalive and PING.  QoS 2 publishes are accepted and
downgraded to QoS 1.

This exists so the whole stack runs with `pip install -r requirements.txt` and
nothing else.  If you later need TLS, ACL backends, bridging or persistence,
point `config.yaml -> mqtt.embedded: false` at Mosquitto or EMQX instead --
the rest of the server does not care which broker it talks to.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

log = logging.getLogger("broker")

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
PUBREC, PUBREL, PUBCOMP, SUBSCRIBE = 5, 6, 7, 8
SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ = 9, 10, 11, 12
PINGRESP, DISCONNECT = 13, 14


def encode_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def encode_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def topic_matches(filt: str, topic: str) -> bool:
    """MQTT wildcard match. `+` = one level, `#` = rest of the tree."""
    f = filt.split("/")
    t = topic.split("/")
    for i, seg in enumerate(f):
        if seg == "#":
            # `#` must be last, and wildcards never match $SYS-style topics
            return i == len(f) - 1 and not (i == 0 and t and t[0].startswith("$"))
        if i >= len(t):
            return False
        if seg == "+":
            if i == 0 and t[0].startswith("$"):
                return False
            continue
        if seg != t[i]:
            return False
    return len(f) == len(t)


@dataclass(eq=False)  # identity semantics: sessions live in a set, and two
                      # distinct connections are never "equal"
class Session:
    client_id: str
    writer: asyncio.StreamWriter
    keepalive: int = 60
    username: str = ""
    subs: List[Tuple[str, int]] = field(default_factory=list)
    will: Optional[Tuple[str, bytes, int, bool]] = None  # topic, payload, qos, retain
    last_rx: float = field(default_factory=time.monotonic)
    packet_id: int = 0
    alive: bool = True

    def next_packet_id(self) -> int:
        self.packet_id = (self.packet_id % 65535) + 1
        return self.packet_id

    def wants(self, topic: str) -> Optional[int]:
        """Highest granted QoS among matching subscriptions, or None."""
        best = None
        for filt, qos in self.subs:
            if topic_matches(filt, topic):
                best = qos if best is None else max(best, qos)
        return best


class MqttBroker:
    def __init__(self, host: str = "0.0.0.0", port: int = 1883,
                 username: Optional[str] = None, password: Optional[str] = None,
                 authenticate: Optional[Callable[[str, str], bool]] = None,
                 authorize: Optional[Callable[[str, str, str], bool]] = None):
        """
        authenticate(username, password) -> bool
            When set, every client MUST present valid credentials. Called on the
            event loop during CONNECT, so it has to be fast and non-blocking --
            the registry answers from an in-memory cache.
        authorize(username, topic, action) -> bool
            action is "publish" or "subscribe". Without this, an authenticated
            robot could still publish to another robot's topic, which would make
            the archive forgeable and therefore useless as evidence.
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.authenticate = authenticate
        self.authorize = authorize
        self.sessions: Set[Session] = set()
        self.retained: Dict[str, Tuple[bytes, int]] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self.stats = {"connects": 0, "publishes": 0, "bytes_in": 0,
                      "auth_failures": 0, "acl_denied": 0, "kicked": 0}
        # in-process subscribers: (topic filter, callback). See subscribe_inproc.
        self._hooks: List[Tuple[str, Callable[[str, bytes], None]]] = []

    def subscribe_inproc(self, topic_filter: str,
                         callback: Callable[[str, bytes], None]) -> None:
        """Receive matching messages directly, without a loopback TCP client.

        Avoids the trap where a second broker already owns 127.0.0.1:1883 and
        quietly swallows the loopback subscription.
        """
        self._hooks.append((topic_filter, callback))

    # ---------------------------------------------------------------- serving

    async def start(self) -> None:
        try:
            self._server = await asyncio.start_server(self._handle, self.host, self.port)
        except OSError as exc:
            # Binding to a specific address couples startup to that interface
            # existing. Say which one, instead of surfacing a bare errno.
            log.error("cannot bind MQTT broker to %s:%d -- %s", self.host, self.port, exc)
            if self.host not in ("0.0.0.0", "::", ""):
                log.error("is that interface up? (Tailscale connected?) "
                          "set mqtt.bind: 0.0.0.0 to listen everywhere instead")
            raise
        scope = "ALL interfaces" if self.host in ("0.0.0.0", "::", "") else "this address only"
        log.info("embedded MQTT broker listening on %s:%d (%s)",
                 self.host, self.port, scope)
        if self.host in ("0.0.0.0", "::", ""):
            log.warning("broker is reachable from every network on this host -- "
                        "a client on the LAN bypasses Tailscale entirely")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for s in list(self.sessions):
            s.alive = False
            try:
                s.writer.close()
            except Exception:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        session: Optional[Session] = None
        try:
            session = await self._do_connect(reader, writer)
            if session is None:
                return
            self.sessions.add(session)
            self.stats["connects"] += 1
            log.info("client %r connected from %s", session.client_id, peer)
            await self._serve(reader, session)
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("broker connection error from %s", peer)
        finally:
            if session is not None:
                self.sessions.discard(session)
                if session.alive and session.will:  # ungraceful close -> fire will
                    topic, payload, qos, retain = session.will
                    await self.publish(topic, payload, qos, retain)
                log.info("client %r disconnected", session.client_id)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_packet(self, reader: asyncio.StreamReader) -> Tuple[int, bytes]:
        (b0,) = await reader.readexactly(1)
        length, mult = 0, 1
        for _ in range(4):
            (byte,) = await reader.readexactly(1)
            length += (byte & 0x7F) * mult
            if not byte & 0x80:
                break
            mult *= 128
        else:
            raise ValueError("malformed remaining length")
        body = await reader.readexactly(length) if length else b""
        self.stats["bytes_in"] += 2 + length
        return b0, body

    async def _do_connect(self, reader, writer) -> Optional[Session]:
        b0, body = await self._read_packet(reader)
        if b0 >> 4 != CONNECT:
            return None
        i = 0
        plen = int.from_bytes(body[i:i + 2], "big"); i += 2
        proto = body[i:i + plen].decode("utf-8", "replace"); i += plen
        level = body[i]; i += 1
        flags = body[i]; i += 1
        keepalive = int.from_bytes(body[i:i + 2], "big"); i += 2

        if proto not in ("MQTT", "MQIsdp") or level not in (3, 4):
            # 0x01 = unacceptable protocol version
            writer.write(bytes([CONNACK << 4, 2, 0, 0x01]))
            await writer.drain()
            return None

        def take_str() -> str:
            nonlocal i
            n = int.from_bytes(body[i:i + 2], "big"); i += 2
            v = body[i:i + n].decode("utf-8", "replace"); i += n
            return v

        def take_bytes() -> bytes:
            nonlocal i
            n = int.from_bytes(body[i:i + 2], "big"); i += 2
            v = body[i:i + n]; i += n
            return v

        client_id = take_str() or f"anon-{int(time.time()*1000) % 100000}"
        will = None
        if flags & 0x04:
            wt = take_str()
            wp = take_bytes()
            will = (wt, wp, (flags >> 3) & 0x03, bool(flags & 0x20))
        user = take_str() if flags & 0x80 else None
        pwd = take_bytes().decode("utf-8", "replace") if flags & 0x40 else None

        peer = writer.get_extra_info("peername")
        if self.authenticate is not None:
            if not user or not self.authenticate(user, pwd or ""):
                self.stats["auth_failures"] += 1
                writer.write(bytes([CONNACK << 4, 2, 0, 0x05]))  # not authorized
                await writer.drain()
                log.warning("rejected %r (user=%r) from %s: bad credentials",
                            client_id, user, peer)
                return None
        elif self.username is not None and (user != self.username or pwd != self.password):
            self.stats["auth_failures"] += 1
            writer.write(bytes([CONNACK << 4, 2, 0, 0x05]))
            await writer.drain()
            log.warning("rejected client %r: bad credentials", client_id)
            return None

        writer.write(bytes([CONNACK << 4, 2, 0, 0x00]))
        await writer.drain()
        return Session(client_id=client_id, writer=writer, keepalive=keepalive,
                       will=will, username=user or "")

    async def _serve(self, reader: asyncio.StreamReader, session: Session) -> None:
        # keepalive: spec says disconnect after 1.5x the client's declared interval
        timeout = session.keepalive * 1.5 if session.keepalive else None
        while session.alive:
            try:
                b0, body = await asyncio.wait_for(self._read_packet(reader), timeout)
            except asyncio.TimeoutError:
                log.info("client %r keepalive expired", session.client_id)
                return
            ptype, flags = b0 >> 4, b0 & 0x0F
            session.last_rx = time.monotonic()

            if ptype == PUBLISH:
                await self._on_publish(session, flags, body)
            elif ptype == SUBSCRIBE:
                await self._on_subscribe(session, body)
            elif ptype == UNSUBSCRIBE:
                await self._on_unsubscribe(session, body)
            elif ptype == PINGREQ:
                self._write(session, bytes([PINGRESP << 4, 0]))
            elif ptype == DISCONNECT:
                session.will = None  # graceful close discards the will
                session.alive = False
                return
            elif ptype == PUBREL:
                pid = int.from_bytes(body[0:2], "big")
                self._write(session, bytes([PUBCOMP << 4, 2]) + pid.to_bytes(2, "big"))
            elif ptype == PUBACK:
                pass  # we do not retransmit QoS1; nothing to reconcile
            else:
                log.debug("ignoring packet type %d from %r", ptype, session.client_id)

    async def _on_publish(self, session: Session, flags: int, body: bytes) -> None:
        qos = (flags >> 1) & 0x03
        retain = bool(flags & 0x01)
        i = 0
        tlen = int.from_bytes(body[i:i + 2], "big"); i += 2
        topic = body[i:i + tlen].decode("utf-8", "replace"); i += tlen
        pid = None
        if qos > 0:
            pid = int.from_bytes(body[i:i + 2], "big"); i += 2
        payload = body[i:]

        # Acknowledge before the ACL check: MQTT 3.1.1 has no "publish denied"
        # response, so withholding the ack would just make the client retry
        # forever. Mosquitto behaves the same way -- ack, then drop.
        if qos == 1 and pid is not None:
            self._write(session, bytes([PUBACK << 4, 2]) + pid.to_bytes(2, "big"))
        elif qos == 2 and pid is not None:
            self._write(session, bytes([PUBREC << 4, 2]) + pid.to_bytes(2, "big"))

        if self.authorize is not None and not self.authorize(session.username, topic, "publish"):
            self.stats["acl_denied"] += 1
            log.warning("ACL denied publish: user=%r topic=%r", session.username, topic)
            return

        await self.publish(topic, payload, min(qos, 1), retain)

    async def _on_subscribe(self, session: Session, body: bytes) -> None:
        pid = int.from_bytes(body[0:2], "big")
        i = 2
        granted = bytearray()
        new_filters: List[Tuple[str, int]] = []
        while i < len(body):
            n = int.from_bytes(body[i:i + 2], "big"); i += 2
            filt = body[i:i + n].decode("utf-8", "replace"); i += n
            qos = min(body[i] & 0x03, 1); i += 1
            if self.authorize is not None and \
                    not self.authorize(session.username, filt, "subscribe"):
                self.stats["acl_denied"] += 1
                log.warning("ACL denied subscribe: user=%r filter=%r",
                            session.username, filt)
                granted.append(0x80)      # SUBACK failure code
                continue
            session.subs = [(f, q) for f, q in session.subs if f != filt]
            session.subs.append((filt, qos))
            new_filters.append((filt, qos))
            granted.append(qos)

        self._write(session, bytes([SUBACK << 4]) + encode_length(2 + len(granted))
                    + pid.to_bytes(2, "big") + bytes(granted))

        # replay retained messages that match the fresh subscriptions
        for topic, (payload, rqos) in list(self.retained.items()):
            for filt, qos in new_filters:
                if topic_matches(filt, topic):
                    self._deliver(session, topic, payload, min(rqos, qos), retain=True)
                    break

    async def _on_unsubscribe(self, session: Session, body: bytes) -> None:
        pid = int.from_bytes(body[0:2], "big")
        i = 2
        while i < len(body):
            n = int.from_bytes(body[i:i + 2], "big"); i += 2
            filt = body[i:i + n].decode("utf-8", "replace"); i += n
            session.subs = [(f, q) for f, q in session.subs if f != filt]
        self._write(session, bytes([UNSUBACK << 4, 2]) + pid.to_bytes(2, "big"))

    # -------------------------------------------------------------- publishing

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        """Route a message to every matching subscriber."""
        self.stats["publishes"] += 1
        if retain:
            if payload:
                self.retained[topic] = (payload, qos)
            else:
                self.retained.pop(topic, None)  # empty retained payload clears it
        for session in list(self.sessions):
            granted = session.wants(topic)
            if granted is not None:
                self._deliver(session, topic, payload, min(qos, granted), retain=False)

        for topic_filter, callback in self._hooks:
            if topic_matches(topic_filter, topic):
                try:
                    callback(topic, payload)
                except Exception:
                    log.exception("in-process subscriber failed on %s", topic)

    def _deliver(self, session: Session, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        header = PUBLISH << 4 | (qos << 1) | (1 if retain else 0)
        var = encode_string(topic)
        if qos > 0:
            var += session.next_packet_id().to_bytes(2, "big")
        body = var + payload
        self._write(session, bytes([header]) + encode_length(len(body)) + body)

    def kick(self, username: str) -> int:
        """Drop every live connection for a username.

        Revocation has to take effect now, not at the next reconnect -- a robot
        that is already connected would otherwise keep publishing indefinitely.
        """
        n = 0
        for session in list(self.sessions):
            if session.username == username:
                session.alive = False
                session.will = None      # revocation is not a crash; no LWT
                try:
                    session.writer.close()
                except Exception:
                    pass
                self.sessions.discard(session)
                n += 1
        if n:
            self.stats["kicked"] += n
            log.warning("kicked %d live session(s) for revoked user %r", n, username)
        return n

    def _write(self, session: Session, data: bytes) -> None:
        try:
            session.writer.write(data)
        except Exception:
            session.alive = False
