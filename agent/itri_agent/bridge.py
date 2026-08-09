"""The relay: vehicle's own broker -> fleet server.

Deliberately ignorant. It does not know what any topic means; it forwards them
under their original names and lets the server decide how to display them. That
is what makes adding a new chassis model a configuration step instead of a
development one.

Two uplink streams:
  fleet/<id>/raw     batches of [topic, ts, value] -- the full archive
  fleet/<id>/status  optional mapped fields, so the dashboard cards light up
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import paho.mqtt.client as mqtt

from .config import should_relay
from .discover import decode

log = logging.getLogger("bridge")


class Bridge:
    def __init__(self, cfg: Dict[str, Any], cred: Dict[str, Any]):
        self.cfg = cfg
        self.cred = cred
        self.robot_id = cred["robot_id"]

        self.buffer: Deque[list] = deque(maxlen=int(cfg["buffer_max"]))
        self._lock = threading.Lock()
        self._last: Dict[str, Any] = {}       # topic -> last relayed value
        self._last_at: Dict[str, float] = {}  # topic -> last relayed time
        self._mapped: Dict[str, Any] = {}     # dashboard fields, latest wins

        self.seen = 0
        self.relayed = 0
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
        self.seen += 1
        topic = msg.topic
        if not should_relay(topic, self.cfg.get("include") or [],
                            self.cfg.get("exclude") or []):
            return
        if len(msg.payload) > int(self.cfg["max_payload_bytes"]):
            self.skipped_big += 1
            return

        now = time.time()
        min_gap = 1.0 / max(float(self.cfg["max_rate_hz"]), 0.001)
        if now - self._last_at.get(topic, 0.0) < min_gap:
            self.skipped_rate += 1
            return

        value, _is_bin = decode(msg.payload)

        if self.cfg.get("on_change_only"):
            prev = self._last.get(topic, _MISSING)
            if prev is not _MISSING and _same(prev, value, float(self.cfg["deadband"])):
                self.skipped_same += 1
                return

        self._last[topic] = value
        self._last_at[topic] = now

        with self._lock:
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped_buffer += 1
            self.buffer.append([topic, round(now, 3), value])
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
            payload = json.dumps({"b": batch}, separators=(",", ":"),
                                 ensure_ascii=False, default=str).encode("utf-8")
            info = self.up.publish(f"fleet/{self.robot_id}/raw", payload, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
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
            for c in (self.local, self.up):
                c.loop_stop()
                try:
                    c.disconnect()
                except Exception:
                    pass

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            buffered = len(self.buffer)
        return {
            "robot_id": self.robot_id,
            "uplink": self.uplink_up,
            "seen": self.seen,
            "relayed": self.relayed,
            "buffered": buffered,
            "batches": self.sent_batches,
            "skipped_rate": self.skipped_rate,
            "skipped_same": self.skipped_same,
            "skipped_big": self.skipped_big,
            "dropped": self.dropped_buffer,
            "topics": len(self._last),
        }


class _Missing:
    pass


_MISSING = _Missing()


def _same(a: Any, b: Any, deadband: float) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= deadband
    return a == b
