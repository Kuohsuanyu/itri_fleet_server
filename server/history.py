"""Telemetry archival: memory buffer -> batched COPY into PostgreSQL.

The contract with the rest of the server is that this layer is allowed to fail.
`FleetState` and the dashboard never read from here, so a database outage costs
you history, not monitoring. While the DB is down samples pile up in a bounded
ring buffer and are flushed once it returns; if the outage outlasts the buffer
the oldest samples are dropped and counted, never silently lost.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

from .db import Database
from .state import Robot

log = logging.getLogger("history")


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


# Vendors publish booleans as True/true/1/on/ON with no consistency, and a flag
# you cannot chart is a flag you cannot investigate. Coerce here rather than
# asking the agent to guess -- the agent stays dumb, and old agents benefit too.
_TRUE = {"true", "1", "on", "yes", "t"}
_FALSE = {"false", "0", "off", "no", "f"}


def _numeric(value: Any) -> Optional[float]:
    """Best-effort scalar for the indexed `num` column. None if not scalar."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        s = value.strip()
        if not s or len(s) > 32:
            return None
        low = s.lower()
        if low in _TRUE:
            return 1.0
        if low in _FALSE:
            return 0.0
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


class TelemetryWriter:
    def __init__(self, db: Database, flush_interval: float = 1.0,
                 buffer_max: int = 200_000, event_buffer_max: int = 20_000,
                 topic_buffer_max: int = 400_000,
                 disk_warn_pct: float = 15.0, disk_critical_pct: float = 5.0,
                 maintenance_interval: float = 3600.0,
                 clock_correct_s: float = 2.0,
                 max_backfill_s: float = 7 * 86400.0,
                 max_future_s: float = 300.0):
        self.db = db
        self.flush_interval = max(float(flush_interval), 0.2)
        self.buffer: Deque[tuple] = deque(maxlen=buffer_max)
        self.events: Deque[tuple] = deque(maxlen=event_buffer_max)
        self.topics: Deque[tuple] = deque(maxlen=topic_buffer_max)
        # (robot, topic) -> [count, last_value, last_seen, last_changed]
        self._catalog: Dict[tuple, list] = {}
        self.topics_written = 0
        self._last_state: Dict[str, str] = {}
        self._last_errors: Dict[str, Tuple[str, ...]] = {}
        self._seen_ids: set[str] = set()

        self.written = 0
        self.events_written = 0
        self.dropped = 0
        self.flush_failures = 0
        self.last_flush_ms = 0.0
        self.last_flush_at = 0.0
        self._tasks: List[asyncio.Task] = []

        self.disk_warn_pct = float(disk_warn_pct)
        self.disk_critical_pct = float(disk_critical_pct)
        self.maintenance_interval = max(float(maintenance_interval), 60.0)
        self.disk: Dict[str, Any] = {}
        self.emergency_drops = 0
        self._disk_warned = False

        self.clock_correct_s = float(clock_correct_s)
        self.max_backfill_s = float(max_backfill_s)
        self.max_future_s = float(max_future_s)
        self.skew: Dict[str, float] = {}
        self._skew_warned: set[str] = set()
        self.clamped = 0

    # ------------------------------------------------------------- recording

    def record(self, robot: Robot) -> None:
        """Called for every accepted telemetry sample. Must be cheap and sync."""
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped += 1          # deque discards the oldest for us
        self.buffer.append((
            robot.id, _ts(robot.last_seen), robot.battery, robot.state,
            robot.v, robot.w, robot.x, robot.y, robot.yaw,
            robot.temp, robot.wifi, robot.odom,
            Jsonb(robot.extra) if robot.extra else None,
        ))
        self._seen_ids.add(robot.id)

        # State transitions and new faults are the audit trail, not the firehose
        prev = self._last_state.get(robot.id)
        if prev != robot.state:
            self._last_state[robot.id] = robot.state
            if prev is not None:
                self.note_event(robot.id, "state",
                                {"from": prev, "to": robot.state},
                                "warn" if robot.state in ("error", "estop") else "info")

        errs = tuple(robot.errors)
        if errs != self._last_errors.get(robot.id, ()):
            self._last_errors[robot.id] = errs
            if errs:
                self.note_event(robot.id, "error", {"errors": list(errs)}, "critical")
            elif prev is not None:
                self.note_event(robot.id, "error", {"cleared": True}, "info")

    def record_topics(self, robot_id: str, batch: list,
                      skew: Optional[float] = None) -> None:
        """A relayed batch: [[topic, ts, value, flag], ...] (flag optional).

        Values arrive as whatever the source published. A bare number is stored
        in `num` so it can be charted from an indexed column; anything else
        lands in `payload` as jsonb. Nothing is interpreted or discarded.

        flag 0 = the value changed, 1 = an unchanged value resent as a
        heartbeat. Storing it is what lets a query distinguish "this reading
        has not moved" from "this sensor stopped reporting" -- with
        on_change_only those look identical otherwise.

        `skew` is (vehicle clock - server clock) for this batch, measured by
        the router. Timestamps are corrected by it before storage; see
        _correct() for why that is not optional.
        """
        now = time.time()
        recv = _ts(now)
        correction = 0.0
        if skew is not None and abs(skew) >= self.clock_correct_s:
            correction = skew
            self._note_skew(robot_id, skew)
        skew_ms = int(skew * 1000) if skew is not None else None
        if skew_ms is not None:
            skew_ms = max(-2_147_483_648, min(2_147_483_647, skew_ms))

        for item in batch:
            try:
                topic, ts, value = item[0], item[1], item[2]
            except (TypeError, IndexError):
                self.dropped += 1
                continue
            if not isinstance(topic, str) or not topic:
                self.dropped += 1
                continue
            try:
                flag = int(item[3]) if len(item) > 3 else 0
            except (TypeError, ValueError):
                flag = 0

            try:
                raw_ts = float(ts) if ts else now
            except (TypeError, ValueError):
                raw_ts = now
            stamp = self._correct(raw_ts, correction, now)

            num = _numeric(value)

            if len(self.topics) == self.topics.maxlen:
                self.dropped += 1
            self.topics.append((robot_id, _ts(stamp), recv, skew_ms,
                                topic[:500], num, Jsonb(value), flag))

            seen = self._catalog.setdefault((robot_id, topic[:500]),
                                            [0, "", 0.0, 0.0])
            seen[0] += 1
            seen[1] = str(value)[:200]
            seen[2] = stamp                                    # last_seen
            if flag == 0:
                seen[3] = stamp                                # last_changed

    def _correct(self, raw_ts: float, correction: float, now: float) -> float:
        """Map a vehicle timestamp onto the server's timeline.

        Two separate jobs, and they are easy to conflate:

        1. Subtracting the measured skew lines up 50 vehicles whose clocks
           disagree. Without it, "what happened across the fleet at 14:32" is
           unanswerable -- each robot means a different 14:32. Subtracting
           rather than replacing preserves the relative spacing within a
           batch and keeps genuine backfill (samples buffered during an
           outage) at its real age.

        2. The hard clamp is not about accuracy, it is about the partition
           key. `ts` decides which daily partition a row lands in. A robot
           whose clock reads 2031 sends rows outside every managed range, so
           they fall into the DEFAULT partition -- which retention skips,
           because dropping it would break inserts. Those rows then live
           forever. One misconfigured vehicle silently defeats the whole
           retention design, and nothing looks wrong until the disk fills.
        """
        stamp = raw_ts - correction
        floor = now - self.max_backfill_s
        ceiling = now + self.max_future_s
        if stamp < floor or stamp > ceiling:
            self.clamped += 1
            return now
        return stamp

    def _note_skew(self, robot_id: str, skew: float) -> None:
        """One event per robot per crossing, not one per batch."""
        self.skew[robot_id] = skew
        if robot_id in self._skew_warned:
            return
        self._skew_warned.add(robot_id)
        log.warning("%s clock is %.1fs %s the server -- timestamps corrected, "
                    "but fix NTP on the vehicle", robot_id, abs(skew),
                    "ahead of" if skew > 0 else "behind")
        self.note_event(robot_id, "clock",
                        {"msg": "車輛時鐘與伺服器不同步,時間戳已校正",
                         "skew_s": round(skew, 2)}, "warning")

    def note_event(self, robot_id: Optional[str], kind: str,
                   detail: Optional[Dict[str, Any]] = None,
                   severity: str = "info") -> None:
        self.events.append((robot_id, datetime.now(timezone.utc), kind, severity,
                            Jsonb(detail) if detail else None))

    def note_presence(self, robot_id: str, online: bool) -> None:
        self.note_event(robot_id, "online" if online else "offline",
                        None, "info" if online else "warn")

    # --------------------------------------------------------------- flushing

    async def _flush_once(self) -> None:
        if not self.buffer and not self.events and not self.topics:
            return
        if not self.db.ready:
            if not await self.db.reconnect():
                return                      # keep buffering, try again next tick

        t0 = time.perf_counter()
        batch = list(self.buffer)
        self.buffer.clear()
        ev_batch = list(self.events)
        self.events.clear()
        topic_batch = list(self.topics)
        self.topics.clear()
        catalog = [(rid, topic, n, last, seen, changed)
                   for (rid, topic), (n, last, seen, changed)
                   in self._catalog.items()]
        self._catalog.clear()

        try:
            if batch:
                await self.db.copy_telemetry(batch)
                self.written += len(batch)
            if topic_batch:
                await self.db.copy_topic_samples(topic_batch)
                self.topics_written += len(topic_batch)
            if catalog:
                await self.db.upsert_catalog(catalog)
            if ev_batch:
                await self.db.insert_events(ev_batch)
                self.events_written += len(ev_batch)
            if self._seen_ids:
                await self.db.touch_last_seen(list(self._seen_ids))
                self._seen_ids.clear()
        except Exception as exc:
            self.flush_failures += 1
            self.db.ready = False
            self.db.last_error = f"{type(exc).__name__}: {exc}"
            # put the batches back at the front so ordering survives the retry
            self.buffer.extendleft(reversed(batch))
            self.events.extendleft(reversed(ev_batch))
            self.topics.extendleft(reversed(topic_batch))
            for rid, topic, n, last, seen, changed in catalog:
                self._catalog[(rid, topic)] = [n, last, seen, changed]
            log.warning("flush failed (%d telemetry + %d topic rows re-queued): %s",
                        len(batch), len(topic_batch), exc)
            return

        self.last_flush_ms = (time.perf_counter() - t0) * 1000
        self.last_flush_at = time.time()

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self._flush_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("flush loop error")

    async def _maintenance_loop(self) -> None:
        """Roll partitions forward, drop expired ones, watch the disk.

        Hourly for partitions; the disk is checked every cycle too, because
        retention alone does not bound the disk. Retention bounds *time*. If
        the incoming rate doubles, or someone relays a 1 kHz topic, 31 days of
        data is simply twice as large, and the first symptom of that is
        PostgreSQL refusing writes.
        """
        while True:
            try:
                if self.db.ready:
                    await self.db.ensure_partitions()
                    await self.db.drop_expired_partitions()
                    await self._check_disk()
                await asyncio.sleep(self.maintenance_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("partition maintenance error")
                await asyncio.sleep(300)

    async def _check_disk(self) -> None:
        info = await self.db.disk()
        if not info:
            return
        self.disk = info
        free = info["free_pct"]

        if free <= self.disk_critical_pct:
            # Shed one day at a time, re-measuring after each. Dropping
            # everything down to the target in one pass would happily delete a
            # week because the space had not been reclaimed yet mid-loop.
            dropped = await self.db.emergency_drop_oldest_day()
            if dropped:
                self.emergency_drops += 1
                log.error("DISK CRITICAL %.1f%% free -- dropped %s",
                          free, ", ".join(dropped))
                self.note_event(None, "disk",
                                {"msg": "磁碟空間危急,已刪除最舊一天的資料",
                                 "free_pct": free, "dropped": dropped},
                                "critical")
            else:
                log.error("DISK CRITICAL %.1f%% free and nothing left to drop",
                          free)
                self.note_event(None, "disk",
                                {"msg": "磁碟空間危急,但已經沒有可刪的分割",
                                 "free_pct": free}, "critical")
        elif free <= self.disk_warn_pct:
            # Warn once per crossing, not once per hour forever.
            if not self._disk_warned:
                self._disk_warned = True
                log.warning("disk low: %.1f%% free on %s", free, info["path"])
                self.note_event(None, "disk",
                                {"msg": "磁碟空間偏低", "free_pct": free,
                                 "path": info["path"]}, "warning")
        else:
            self._disk_warned = False

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._flush_loop()),
                       asyncio.create_task(self._maintenance_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        await self._flush_once()   # best effort: do not throw away a final second

    # ------------------------------------------------------------------ stats

    def stats(self) -> Dict[str, Any]:
        return {
            "db_ready": self.db.ready,
            "db_error": self.db.last_error,
            "disk": self.disk,
            # Vehicles whose clock differs from the server's by more than
            # clock_correct_s. Their timestamps are corrected on the way in,
            # but the underlying NTP problem is theirs to fix.
            "clock_skew": {k: round(v, 2) for k, v in self.skew.items()},
            "clock_clamped": self.clamped,
            "disk_warn_pct": self.disk_warn_pct,
            "disk_critical_pct": self.disk_critical_pct,
            "emergency_drops": self.emergency_drops,
            "buffered": len(self.buffer),
            "buffer_max": self.buffer.maxlen,
            "buffer_pct": round(len(self.buffer) / self.buffer.maxlen * 100, 1),
            "topics_buffered": len(self.topics),
            "topics_written": self.topics_written,
            "rows_written": self.written,
            "events_written": self.events_written,
            "rows_dropped": self.dropped,
            "flush_failures": self.flush_failures,
            "last_flush_ms": round(self.last_flush_ms, 1),
            "last_flush_age_s": round(time.time() - self.last_flush_at, 1)
                                 if self.last_flush_at else None,
        }


# ------------------------------------------------------------------ queries

async def query_telemetry(db: Database, robot_id: Optional[str],
                          start: datetime, end: datetime,
                          limit: int = 5000,
                          bucket_s: Optional[float] = None) -> List[Dict[str, Any]]:
    """Raw samples, or time-bucketed averages when the window is wide.

    Bucketing happens in SQL so a month-long query returns a few thousand rows
    instead of ten million.
    """
    params: List[Any] = []
    where = ["ts >= %s", "ts < %s"]
    params += [start, end]
    if robot_id:
        where.append("robot_id = %s")
        params.append(robot_id)
    clause = " AND ".join(where)

    if bucket_s:
        params.append(limit)
        return await db.fetch(f"""
            SELECT robot_id,
                   to_timestamp(floor(extract(epoch FROM ts) / {float(bucket_s)})
                                * {float(bucket_s)}) AS ts,
                   avg(battery)::real AS battery,
                   avg(v)::real       AS v,
                   max(abs(w))::real  AS w_peak,
                   avg(temp)::real    AS temp,
                   count(*)           AS samples
            FROM telemetry
            WHERE {clause}
            GROUP BY robot_id, 2
            ORDER BY 2
            LIMIT %s
        """, params)

    params.append(limit)
    return await db.fetch(f"""
        SELECT robot_id, ts, battery, state, v, w, temp, wifi, odom, extra
        FROM telemetry
        WHERE {clause}
        ORDER BY ts
        LIMIT %s
    """, params)


async def query_events(db: Database, robot_id: Optional[str] = None,
                       kind: Optional[str] = None, severity: Optional[str] = None,
                       start: Optional[datetime] = None, end: Optional[datetime] = None,
                       limit: int = 500) -> List[Dict[str, Any]]:
    where, params = ["1=1"], []
    if robot_id:
        where.append("robot_id = %s"); params.append(robot_id)
    if kind:
        where.append("kind = %s"); params.append(kind)
    if severity:
        where.append("severity = %s"); params.append(severity)
    if start:
        where.append("ts >= %s"); params.append(start)
    if end:
        where.append("ts < %s"); params.append(end)
    params.append(limit)
    return await db.fetch(f"""
        SELECT id, robot_id, ts, kind, severity, detail
        FROM events WHERE {" AND ".join(where)}
        ORDER BY ts DESC LIMIT %s
    """, params)
