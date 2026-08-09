"""The relay: vehicle's own broker -> fleet server.

Deliberately ignorant. It does not know what any topic means; it forwards them
under their original names and lets the server decide how to display them. That
is what makes adding a new chassis model a configuration step instead of a
development one.

Two uplink streams:
  fleet/<id>/samples  batches of [topic, ts, value, flag] -- the archive
  fleet/<id>/status   optional mapped fields, so the dashboard cards light up

`samples` used to be called `raw`, which was a lie: this stream is throttled,
deduplicated and size-capped, so it is a summary of the source, not the raw
signal. Genuinely unfiltered data is what recorder.py keeps on the vehicle.
The old name is still published when `legacy_raw_topic` is set, because a
server that predates the rename only subscribes to fleet/+/raw.

Every batch carries an envelope so the server can tell a retransmission from
new data:

    {"v": 1, "id": ..., "boot": ..., "seq": 41, "ts": ..., "b": [...]}

MQTT QoS 1 is at-least-once. A reconnect mid-publish redelivers the batch, and
without (boot, seq) the server has no way to know it already stored those rows
-- they would appear as real duplicate samples in the archive and skew any
count or average computed over them. boot_id changes on every agent start, so
seq never has to survive a restart or be persisted.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import paho.mqtt.client as mqtt

from .config import resolve_source, should_relay
from .discover import decode

log = logging.getLogger("bridge")

SCHEMA_VERSION = 1

# Sample flags, sent as the 4th element of each row.
FLAG_CHANGE = 0      # the value differs from the last one relayed
FLAG_HEARTBEAT = 1   # identical value, resent to prove the source is alive


class Bridge:
    def __init__(self, cfg: Dict[str, Any], cred: Dict[str, Any]):
        self.cfg = cfg
        self.cred = cred
        self.robot_id = cred["robot_id"]

        self.buffer: Deque[list] = deque(maxlen=int(cfg["buffer_max"]))
        self._lock = threading.Lock()
        self._mapped: Dict[str, Any] = {}     # dashboard fields, latest wins

        # Three different questions, three different clocks. Collapsing them is
        # what makes on_change_only ambiguous: with only "last relayed", a topic
        # that has not appeared for an hour is indistinguishable from one whose
        # value simply has not changed, and "sensor dead" reads as "all normal".
        self._last: Dict[str, Any] = {}          # topic -> last value relayed
        self._changed_at: Dict[str, float] = {}  # topic -> value last differed
        self._seen_at: Dict[str, float] = {}     # topic -> source last produced
        self._sent_at: Dict[str, float] = {}     # topic -> last put on the wire

        # Identifies this run of the agent. Regenerated on every start, which is
        # what lets `seq` restart at 0 without ever colliding with an older run.
        self.boot_id = uuid.uuid4().hex[:12]
        self.seq = 0

        self.recorder = None
        rec = cfg.get("record") or {}
        if rec.get("enabled"):
            from .recorder import Recorder
            self.recorder = Recorder(
                directory=rec.get("dir", "~/.itri-fleet/recordings"),
                rate_hz=float(rec.get("rate_hz", 0) or 0),
                rotate_mb=float(rec.get("rotate_mb", 64)),
                max_gb=float(rec.get("max_gb", 5)),
                compress=bool(rec.get("compress", True)))
            log.info("local recording -> %s (%s)", self.recorder.dir,
                     "全速" if not self.recorder.rate_hz
                     else f"{self.recorder.rate_hz} Hz")

        self.heartbeat_s = float(cfg.get("heartbeat_s", 60) or 0)

        self.seen = 0
        self.relayed = 0
        self.heartbeats = 0
        self.skipped_rate = 0
        self.skipped_same = 0
        self.skipped_big = 0
        self.dropped_buffer = 0
        self.sent_batches = 0
        self.uplink_up = False

        # reverse of cfg["map"]: local topic -> dashboard field
        self.field_of: Dict[str, str] = {
            topic: field for field, topic in (cfg.get("map") or {}).items() if topic
        }
        self.source = resolve_source(cfg)
        self._ros = None

        # `raw` was renamed to `samples`. Publishing to both keeps a vehicle
        # working against a server that has not been updated yet; drop
        # legacy_raw_topic once every server is on the new name, since it
        # doubles this stream's bandwidth.
        self.uplink_topics = [f"fleet/{self.robot_id}/samples"]
        if cfg.get("legacy_raw_topic", True):
            self.uplink_topics.append(f"fleet/{self.robot_id}/raw")

        self.local = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=f"itri-agent-{self.robot_id}")
        lc = cfg["local"]
        if lc.get("username"):
            self.local.username_pw_set(lc["username"], lc.get("password"))
        self.local.on_connect = self._on_local_connect
        self.local.on_message = self._on_local_message
        self.local.reconnect_delay_set(min_delay=1, max_delay=30)

        self.up = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=self.robot_id)
        self.up.username_pw_set(cred["mqtt_username"], cred["mqtt_password"])
        self.up.will_set(f"fleet/{self.robot_id}/lwt", "offline", qos=1, retain=True)
        self.up.on_connect = self._on_up_connect
        self.up.on_disconnect = self._on_up_disconnect
        self.up.reconnect_delay_set(min_delay=1, max_delay=30)

    # ------------------------------------------------------------ local side

    def _on_local_connect(self, c, u, flags, rc, props=None):
        code = int(getattr(rc, "value", rc))
        if code != 0:
            log.error("local broker refused connection: %s", code)
            return
        for filt in self.cfg["subscribe"]:
            c.subscribe(filt, qos=0)
        log.info("subscribed to local broker: %s", ", ".join(self.cfg["subscribe"]))

    def _on_local_message(self, c, u, msg):
        """MQTT path: decode the payload, then hand it to the common filter."""
        if len(msg.payload) > int(self.cfg["max_payload_bytes"]):
            self.seen += 1
            self.skipped_big += 1
            return
        value, _is_bin = decode(msg.payload)
        self.ingest(msg.topic, time.time(), value)

    def ingest(self, topic: str, now: float, value: Any) -> None:
        """Everything the source produced, before any uplink filtering.

        The local recorder hooks in here rather than inside offer(), because the
        whole point of it is to keep what the uplink throws away.
        """
        if self.recorder is not None:
            self.recorder.write(topic, now, value)
        self.offer(topic, now, value)

    def offer(self, topic: str, now: float, value: Any) -> None:
        """Single filter path shared by the MQTT and ROS 2 sources.

        Both sources produce (topic, timestamp, value); everything downstream --
        rate limiting, change detection, buffering, field mapping -- is
        identical, so neither source needs to know about the other.
        """
        self.seen += 1
        if not should_relay(topic, self.cfg.get("include") or [],
                            self.cfg.get("exclude") or []):
            return

        # The source produced something. True even if we go on to drop it, and
        # that is the point: this is the clock that says the sensor is alive.
        self._seen_at[topic] = now

        min_gap = 1.0 / max(float(self.cfg["max_rate_hz"]), 0.001)
        if now - self._sent_at.get(topic, 0.0) < min_gap:
            self.skipped_rate += 1
            return

        prev = self._last.get(topic, _MISSING)
        unchanged = (prev is not _MISSING
                     and _same(prev, value, float(self.cfg["deadband"])))

        flag = FLAG_CHANGE
        if unchanged:
            if not self.cfg.get("on_change_only"):
                pass                      # relaying everything anyway
            elif self.heartbeat_s and \
                    now - self._sent_at.get(topic, 0.0) >= self.heartbeat_s:
                # Resend the identical value so the server can tell "unchanged"
                # from "gone". Without this the archive has a gap that looks the
                # same as a dead sensor, and staleness alerts cannot be written.
                flag = FLAG_HEARTBEAT
                self.heartbeats += 1
            else:
                self.skipped_same += 1
                return
        else:
            self._changed_at[topic] = now

        self._last[topic] = value
        self._sent_at[topic] = now

        with self._lock:
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped_buffer += 1
            self.buffer.append([topic, round(now, 3), value, flag])
        self.relayed += 1

        field = self.field_of.get(topic)
        if field:
            self._mapped[field] = value

    # ----------------------------------------------------------- uplink side

    def _on_up_connect(self, c, u, flags, rc, props=None):
        code = int(getattr(rc, "value", rc))
        self.uplink_up = code == 0
        if not self.uplink_up:
            log.error("fleet server refused credentials (CONNACK %s) -- "
                      "has this robot been revoked? re-run `itri-agent enroll`", code)
            return
        c.publish(f"fleet/{self.robot_id}/lwt", "online", qos=1, retain=True)
        log.info("uplink connected as %s", self.robot_id)

    def _on_up_disconnect(self, c, u, flags, rc, props=None):
        self.uplink_up = False
        log.warning("uplink lost (%s); buffering locally", rc)

    def _flush(self) -> None:
        """Send one batch. Anything not sent stays buffered for the next tick."""
        if not self.uplink_up:
            return
        with self._lock:
            if not self.buffer:
                batch = []
            else:
                n = min(len(self.buffer), int(self.cfg["max_batch"]))
                batch = [self.buffer.popleft() for _ in range(n)]

        if batch:
            self.seq += 1
            envelope = {
                "v": SCHEMA_VERSION,
                "id": self.robot_id,
                "boot": self.boot_id,
                "seq": self.seq,
                "ts": round(time.time(), 3),
                "b": batch,
            }
            payload = json.dumps(envelope, separators=(",", ":"),
                                 ensure_ascii=False, default=str).encode("utf-8")
            ok = True
            for topic in self.uplink_topics:
                info = self.up.publish(topic, payload, qos=1)
                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                    ok = False
            if not ok:
                self.seq -= 1               # this seq was never delivered
                with self._lock:            # put it back, preserving order
                    self.buffer.extendleft(reversed(batch))
                return
            self.sent_batches += 1

        if self._mapped:
            status = dict(self._mapped)
            status["id"] = self.robot_id
            status["ts"] = round(time.time(), 2)
            self.up.publish(f"fleet/{self.robot_id}/status",
                            json.dumps(status, separators=(",", ":"), default=str),
                            qos=0)

    # ---------------------------------------------------------------- driver

    def run(self, on_tick=None) -> None:
        if self.source == "ros2":
            from .ros2 import Ros2Source
            self._ros = Ros2Source(
                lambda t, ts, v: self.ingest(t, ts, v),
                include=self.cfg.get("include") or [],
                exclude=self.cfg.get("exclude") or [],
                max_array=int(self.cfg.get("ros_max_array", 8)))
            self._ros.start()
        else:
            lc = self.cfg["local"]
            self.local.connect_async(lc["host"], int(lc["port"]), keepalive=30)
            self.local.loop_start()

        self.up.connect_async(self.cred["mqtt"]["host"], int(self.cred["mqtt"]["port"]),
                              keepalive=30)
        self.up.loop_start()

        period = 1.0 / max(float(self.cfg["publish_hz"]), 0.05)
        try:
            while True:
                time.sleep(period)
                try:
                    self._flush()
                except Exception:
                    log.exception("flush failed; keeping data buffered")
                if on_tick:
                    on_tick(self.stats())
        except KeyboardInterrupt:
            pass
        finally:
            self.up.publish(f"fleet/{self.robot_id}/lwt", "offline", qos=1, retain=True)
            time.sleep(0.3)
            if getattr(self, "_ros", None):
                self._ros.stop()
            if self.recorder is not None:
                self.recorder.stop()
            for c in ((self.up,) if self.source == "ros2" else (self.local, self.up)):
                c.loop_stop()
                try:
                    c.disconnect()
                except Exception:
                    pass

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            buffered = len(self.buffer)
        now = time.time()
        # Topics the source has stopped producing. This is the number that
        # answers "is anything quietly dead?", which relayed/skipped cannot.
        stale_after = max(self.heartbeat_s * 3, 30.0)
        stale = sum(1 for t, at in self._seen_at.items() if now - at > stale_after)
        out = {
            "robot_id": self.robot_id,
            "boot_id": self.boot_id,
            "source": self.source,
            "uplink": self.uplink_up,
            "seen": self.seen,
            "relayed": self.relayed,
            "heartbeats": self.heartbeats,
            "buffered": buffered,
            "batches": self.sent_batches,
            "seq": self.seq,
            "skipped_rate": self.skipped_rate,
            "skipped_same": self.skipped_same,
            "skipped_big": self.skipped_big,
            "dropped": self.dropped_buffer,
            "topics": len(self._seen_at),
            "stale_topics": stale,
        }
        if self.recorder is not None:
            out["recording"] = self.recorder.stats()
        return out


class _Missing:
    pass


_MISSING = _Missing()


def _same(a: Any, b: Any, deadband: float) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= deadband
    return a == b
