"""MQTT -> FleetState bridge.

Two delivery paths share one parser:

* embedded broker  -- `TelemetryRouter` is hooked straight into `MqttBroker`,
  so messages never touch a TCP loopback socket.  This matters: another MQTT
  broker (Mosquitto is a common one) may already own `127.0.0.1:1883` while we
  own `0.0.0.0:1883`, and then a loopback client silently lands on the *other*
  broker -- robots publish to us, the server subscribes to them, and the
  dashboard stays empty.  Going in-process removes that failure entirely.
* external broker  -- `MqttIngest` runs paho on its own thread and hands every
  message to the same router via `call_soon_threadsafe`.

Either way `FleetState` is only ever touched from the asyncio loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional

import paho.mqtt.client as mqtt

from .state import FleetState

log = logging.getLogger("ingest")


class TelemetryRouter:
    """Parses `fleet/<id>/status` and `fleet/<id>/lwt` into FleetState.

    `on_sample` / `on_presence` are where the archival layer hooks in. They are
    called synchronously on the loop thread and must stay cheap -- the history
    writer only appends to an in-memory buffer here.
    """

    def __init__(self, state: FleetState,
                 on_sample: Optional[Callable[[object], None]] = None,
                 on_presence: Optional[Callable[[str, bool], None]] = None,
                 on_raw: Optional[Callable[[str, list, Optional[float]], None]] = None,
                 dedup_window: int = 4096):
        self.state = state
        self.on_sample = on_sample
        self.on_presence = on_presence
        self.on_raw = on_raw
        self.dropped = 0
        self.accepted = 0
        self.raw_batches = 0
        self.raw_samples = 0

        # (robot_id, boot_id, seq) seen recently. An OrderedDict used as a
        # bounded FIFO set: redelivery happens within seconds of the original,
        # so a window of a few thousand batches is far more than enough, and
        # bounding it means a long-running server cannot leak memory here.
        self._recent: "OrderedDict[tuple, None]" = OrderedDict()
        self.dedup_window = max(int(dedup_window), 64)
        self.duplicates = 0
        self.duplicate_samples = 0
        self.unversioned = 0        # batches from agents predating the envelope

        # robot_id -> (vehicle clock - server clock), seconds. Populated from
        # the batch envelope; None for agents that do not send one.
        self.last_skew: Dict[str, float] = {}

    def handle(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        if len(parts) < 3:
            return
        robot_id, kind = parts[1], parts[2]

        if kind == "lwt":
            text = payload.decode("utf-8", "replace").strip().strip('"').lower()
            up = text in ("online", "1", "true", "up")
            existing = self.state.robots.get(robot_id)
            was = existing.presence if existing else None
            self.state.set_link(robot_id, up)
            now = self.state.robots[robot_id].presence
            if self.on_presence and was != now:
                self.on_presence(robot_id, now == "ok")
            return

        if kind in ("samples", "raw"):
            self._apply_samples(robot_id, payload)
            return

        if kind != "status":
            return

        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.dropped += 1
            return
        if not isinstance(data, dict):
            self.dropped += 1
            return

        rid = str(data.get("id") or robot_id)
        was_online = rid in self.state.robots and self.state.robots[rid].online
        robot = self.state.ingest(rid, data, len(payload))
        self.accepted += 1
        if self.on_presence and not was_online:
            self.on_presence(rid, True)
        if self.on_sample:
            self.on_sample(robot)

    def _apply_samples(self, robot_id: str, payload: bytes) -> None:
        """A batch of the vehicle's own topics, relayed verbatim by itri-agent.

        Shape: {"v":1, "id":..., "boot":..., "seq":n, "ts":..., "b":[[topic, ts,
        value, flag], ...]} -- rows are positional to keep the uplink small; a
        batch of 200 samples is mostly topic strings otherwise. Nothing here
        interprets the topics; that mapping happens server-side, once per
        chassis model.

        Old agents send a bare {"b": [...]} with 3-element rows and no
        envelope; those are accepted, they just cannot be deduplicated.
        """
        try:
            data = json.loads(payload.decode("utf-8"))
            batch = data["b"] if isinstance(data, dict) else data
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            self.dropped += 1
            return
        if not isinstance(batch, list):
            self.dropped += 1
            return

        if isinstance(data, dict):
            boot = data.get("boot")
            seq = data.get("seq")
            if boot is not None and seq is not None:
                # MQTT QoS 1 is at-least-once: a reconnect part way through a
                # publish redelivers the whole batch. Without this the same
                # samples land in the archive twice and every count, average or
                # rate computed over them is wrong -- silently, because
                # duplicate rows look exactly like real ones.
                key = (robot_id, str(boot), int(seq))
                if key in self._recent:
                    self.duplicates += 1
                    self.duplicate_samples += len(batch)
                    return
                self._recent[key] = None
                while len(self._recent) > self.dedup_window:
                    self._recent.popitem(last=False)
            else:
                self.unversioned += 1

        # Clock skew, measured once per batch rather than per row.
        #
        # Per row would be wrong: after an uplink outage the agent flushes
        # samples that are legitimately hours old, and those would read as
        # enormous drift. The envelope's `ts` is stamped at send time (the
        # agent does not even build an envelope while the uplink is down), so
        # envelope-vs-arrival isolates the clock difference from the backlog.
        skew = None
        if isinstance(data, dict):
            sent = data.get("ts")
            if isinstance(sent, (int, float)):
                skew = float(sent) - time.time()
                self.last_skew[robot_id] = skew

        self.raw_batches += 1
        self.raw_samples += len(batch)
        if self.on_raw:
            self.on_raw(robot_id, batch, skew)


class MqttIngest:
    """paho client for an external broker (config `mqtt.embedded: false`)."""

    def __init__(self, router: TelemetryRouter, loop: asyncio.AbstractEventLoop,
                 host: str = "127.0.0.1", port: int = 1883,
                 topic: str = "fleet/+/status", lwt_topic: str = "fleet/+/lwt",
                 username: Optional[str] = None, password: Optional[str] = None,
                 client_id: str = "itri-fleet-server"):
        self.router = router
        self.loop = loop
        self.host = host
        self.port = port
        self.topic = topic
        self.lwt_topic = lwt_topic
        self.connected = False
        self.last_error: Optional[str] = None

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)

    def start(self) -> None:
        try:
            self.client.connect_async(self.host, self.port, keepalive=30)
            self.client.loop_start()
            log.info("MQTT ingest -> %s:%d topic=%s", self.host, self.port, self.topic)
        except Exception as exc:  # never let a broker hiccup kill the web server
            self.last_error = str(exc)
            log.error("MQTT connect failed: %s", exc)

    def stop(self) -> None:
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            self.last_error = str(reason_code)
            log.error("MQTT connect rejected: %s", reason_code)
            return
        self.connected = True
        self.last_error = None
        client.subscribe([(self.topic, 0), (self.lwt_topic, 1)])
        log.info("MQTT connected, subscribed to %s and %s", self.topic, self.lwt_topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        log.warning("MQTT disconnected (%s), paho will retry", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            self.loop.call_soon_threadsafe(self.router.handle, msg.topic, msg.payload)
        except RuntimeError:
            self.router.dropped += 1  # loop already closed during shutdown
