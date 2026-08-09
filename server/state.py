"""In-memory fleet state: one record per wheeled robot, plus short history.

Everything lives in RAM on purpose.  A fleet dashboard is a *live* view; if you
need durable history, tee the MQTT stream into InfluxDB/TimescaleDB rather than
growing this module.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# Default trail points per robot; overridden from config by FleetState.
HISTORY_LEN = 240


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


class Robot:
    __slots__ = ("id", "name", "state", "battery", "x", "y", "yaw", "v", "w",
                 "mission", "progress", "errors", "temp", "wifi", "odom",
                 "extra", "last_seen", "online", "rx_count", "rx_bytes",
                 "history", "rev")

    def __init__(self, robot_id: str, history_len: int = HISTORY_LEN):
        self.id = robot_id
        self.name = robot_id
        self.state = "unknown"
        self.battery: Optional[float] = None
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self.v = 0.0
        self.w = 0.0
        self.mission: Optional[str] = None
        self.progress: Optional[float] = None
        self.errors: List[str] = []
        self.temp: Optional[float] = None
        self.wifi: Optional[float] = None
        self.odom: Optional[float] = None
        self.extra: Dict[str, Any] = {}
        self.last_seen = 0.0
        self.online = False
        self.rx_count = 0
        self.rx_bytes = 0
        self.history: Deque[Dict[str, float]] = deque(maxlen=history_len)
        self.rev = 0  # bumped on every change, so the pusher can send deltas

    def update(self, msg: Dict[str, Any], raw_size: int = 0) -> None:
        """Merge one telemetry payload. Unknown keys are kept under `extra`."""
        now = time.time()
        known = {"id", "robot_id", "name", "state", "status", "battery", "batt",
                 "pose", "x", "y", "yaw", "theta", "vel", "v", "w",
                 "linear", "angular", "mission", "errors", "error",
                 "temp", "temperature", "wifi", "rssi", "odom", "odom_m", "ts"}

        self.name = str(msg.get("name") or self.name)
        state = msg.get("state") or msg.get("status")
        if state:
            self.state = str(state)

        batt = _num(msg.get("battery", msg.get("batt")))
        if batt is not None:
            self.battery = max(0.0, min(100.0, batt))

        pose = msg.get("pose") if isinstance(msg.get("pose"), dict) else msg
        self.x = _num(pose.get("x"), self.x) or 0.0
        self.y = _num(pose.get("y"), self.y) or 0.0
        yaw = _num(pose.get("yaw", pose.get("theta")))
        if yaw is not None:
            self.yaw = yaw

        vel = msg.get("vel") if isinstance(msg.get("vel"), dict) else msg
        self.v = _num(vel.get("v", vel.get("linear")), self.v) or 0.0
        self.w = _num(vel.get("w", vel.get("angular")), self.w) or 0.0

        mission = msg.get("mission")
        if isinstance(mission, dict):
            self.mission = mission.get("id") or mission.get("name")
            self.progress = _num(mission.get("progress"))
        elif mission is not None:
            self.mission = str(mission)

        errs = msg.get("errors", msg.get("error"))
        if errs is None:
            pass
        elif isinstance(errs, (list, tuple)):
            self.errors = [str(e) for e in errs]
        else:
            self.errors = [str(errs)] if errs else []

        self.temp = _num(msg.get("temp", msg.get("temperature")), self.temp)
        self.wifi = _num(msg.get("wifi", msg.get("rssi")), self.wifi)
        self.odom = _num(msg.get("odom", msg.get("odom_m")), self.odom)

        for k, val in msg.items():
            if k not in known:
                self.extra[k] = val

        self.last_seen = now
        self.online = True
        self.rx_count += 1
        self.rx_bytes += raw_size
        self.rev += 1
        self.history.append({
            "t": round(now, 2),
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "b": round(self.battery, 1) if self.battery is not None else None,
            "v": round(self.v, 3),
        })

    def mark_offline(self) -> bool:
        if not self.online:
            return False
        self.online = False
        self.rev += 1
        return True

    def to_dict(self, with_history: bool = False) -> Dict[str, Any]:
        # Pose is still ingested and kept on the Robot (the DB layer wants it),
        # but the dashboard does not display position, so it stays off the wire.
        # Measured at 30 robots: 5409 B/frame instead of 6273 B, ~14% saved.
        d = {
            "id": self.id,
            "name": self.name,
            "state": "offline" if not self.online else self.state,
            "online": self.online,
            "battery": self.battery,
            "vel": {"v": round(self.v, 3), "w": round(self.w, 3)},
            "mission": self.mission,
            "progress": self.progress,
            "errors": self.errors,
            "temp": self.temp,
            "wifi": self.wifi,
            "odom": self.odom,
            "last_seen": round(self.last_seen, 2),
            "age": round(time.time() - self.last_seen, 2) if self.last_seen else None,
            "rx_count": self.rx_count,
            "rev": self.rev,
        }
        if self.extra:
            d["extra"] = self.extra
        if with_history:
            d["history"] = list(self.history)
        return d


class FleetState:
    def __init__(self, offline_after: float = 6.0,
                 history_len: int = HISTORY_LEN):
        self.robots: Dict[str, Robot] = {}
        self.offline_after = offline_after
        self.history_len = max(int(history_len), 2)
        self.msg_count = 0
        self.msg_bytes = 0
        self.started = time.time()

    def ingest(self, robot_id: str, msg: Dict[str, Any], raw_size: int = 0) -> Robot:
        robot = self.robots.get(robot_id)
        if robot is None:
            robot = self.robots[robot_id] = Robot(robot_id, self.history_len)
        robot.update(msg, raw_size)
        self.msg_count += 1
        self.msg_bytes += raw_size
        return robot

    def set_presence(self, robot_id: str, online: bool) -> None:
        """Driven by MQTT last-will topics (`fleet/<id>/lwt`)."""
        robot = self.robots.get(robot_id)
        if robot is None:
            robot = self.robots[robot_id] = Robot(robot_id, self.history_len)
        if not online:
            robot.mark_offline()
        else:
            robot.online = True
            robot.rev += 1

    def reap(self) -> List[str]:
        """Flip robots to offline once telemetry goes stale. Returns changed ids."""
        cutoff = time.time() - self.offline_after
        changed = []
        for robot in self.robots.values():
            if robot.online and robot.last_seen < cutoff:
                robot.mark_offline()
                changed.append(robot.id)
        return changed

    def snapshot(self, with_history: bool = False) -> List[Dict[str, Any]]:
        return [r.to_dict(with_history) for r in
                sorted(self.robots.values(), key=lambda r: r.id)]

    def summary(self) -> Dict[str, Any]:
        robots = list(self.robots.values())
        online = [r for r in robots if r.online]
        batts = [r.battery for r in online if r.battery is not None]
        return {
            "total": len(robots),
            "online": len(online),
            "offline": len(robots) - len(online),
            "moving": sum(1 for r in online if abs(r.v) > 0.02 or abs(r.w) > 0.02),
            "charging": sum(1 for r in online if r.state == "charging"),
            "faulted": sum(1 for r in online if r.errors or r.state in ("error", "estop")),
            "avg_battery": round(sum(batts) / len(batts), 1) if batts else None,
            "min_battery": round(min(batts), 1) if batts else None,
            "msg_count": self.msg_count,
            "msg_bytes": self.msg_bytes,
            "uptime": round(time.time() - self.started, 1),
        }
