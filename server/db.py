"""PostgreSQL access: pool, schema, and daily partition lifecycle.

Implementation note -- why the *sync* driver behind `asyncio.to_thread`:

psycopg's async mode refuses to run on Windows' default ProactorEventLoop, and
the usual workaround (forcing SelectorEventLoop) both diverges from how this
will run on the Linux server later and caps you at select()'s 512 descriptors.
Running the sync driver in a small thread pool behaves identically on both
platforms, keeps every byte of database I/O off the event loop, and costs
nothing at this scale.

The connection is deliberately allowed to be *absent*. The dashboard serves
live state from memory, so a database outage degrades history only; it must
never take monitoring down. Callers check `db.ready`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("db")

SCHEMA_SQL = Path(__file__).with_name("schema.sql")

TELEMETRY_COLS = ("robot_id", "ts", "battery", "state", "v", "w", "x", "y",
                  "yaw", "temp", "wifi", "odom", "extra")
TELEMETRY_TYPES = ["text", "timestamptz", "real", "text", "real", "real", "real",
                   "real", "real", "real", "real", "real", "jsonb"]


class Database:
    def __init__(self, dsn: str, retention_days: int = 31,
                 min_size: int = 1, max_size: int = 6,
                 partition_days_ahead: int = 2):
        self.dsn = dsn
        self.retention_days = max(int(retention_days), 1)
        self.partition_days_ahead = max(int(partition_days_ahead), 0)
        self._pool: Optional[ConnectionPool] = None
        self._min, self._max = min_size, max_size
        self.ready = False
        self.last_error: Optional[str] = None
        self.reconnects = 0
        self._retry_at = 0.0        # monotonic deadline for the next attempt
        self._retry_delay = 1.0     # doubles per failure, capped at 30s
        self._down_since = 0.0

    # ------------------------------------------------------------- lifecycle

    def _open_sync(self) -> None:
        self._pool = ConnectionPool(
            self.dsn, min_size=self._min, max_size=self._max,
            open=False, timeout=10.0, max_idle=300.0,
            kwargs={"application_name": "itri-fleet"},
        )
        self._pool.open(wait=True, timeout=15.0)

    async def start(self) -> None:
        """Open the pool and apply schema. Never raises -- degraded is a mode."""
        try:
            await asyncio.to_thread(self._open_sync)
            await self.ensure_schema()
            await self.ensure_partitions()
            self.ready = True
            self.last_error = None
            log.info("PostgreSQL connected, retention=%d days", self.retention_days)
        except Exception as exc:
            self.ready = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.error("PostgreSQL unavailable (history off, live view unaffected): %s",
                      self.last_error)

    async def stop(self) -> None:
        pool, self._pool = self._pool, None
        self.ready = False
        if pool is not None:
            await asyncio.to_thread(pool.close)

    async def reconnect(self) -> bool:
        """Recover after an outage, on our own backoff schedule.

        psycopg_pool retries internally with a backoff that can grow longer
        than the outage itself, so a plain `pool.connection()` keeps raising
        PoolTimeout well after the server is back. Rather than guess its
        schedule, probe once and rebuild the pool outright if the probe fails.
        """
        now = time.monotonic()
        if now < self._retry_at:
            return False
        self.reconnects += 1
        try:
            if self._pool is None:
                await asyncio.to_thread(self._open_sync)
                await asyncio.to_thread(self._probe_sync)
            else:
                try:
                    await asyncio.to_thread(self._probe_sync)
                except Exception:
                    await asyncio.to_thread(self._recycle_sync)
            if not self.ready:
                await self.ensure_schema()
                await self.ensure_partitions()
                log.info("PostgreSQL reconnected after %.0fs (attempt %d)",
                         now - self._down_since if self._down_since else 0,
                         self.reconnects)
            self.ready = True
            self.last_error = None
            self._retry_delay = 1.0
            self._retry_at = 0.0
            self._down_since = 0.0
            return True
        except Exception as exc:
            self.ready = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            if not self._down_since:
                self._down_since = now
            self._retry_delay = min(self._retry_delay * 2, 30.0)
            self._retry_at = now + self._retry_delay
            return False

    def _probe_sync(self) -> None:
        with self._pool.connection(timeout=5.0) as conn:
            conn.execute("SELECT 1")

    def _recycle_sync(self) -> None:
        """Throw the pool away and build a fresh one."""
        old, self._pool = self._pool, None
        try:
            old.close(timeout=2.0)
        except Exception:
            pass
        self._open_sync()
        self._probe_sync()

    # ---------------------------------------------------------------- schema

    def _ensure_schema_sync(self) -> None:
        sql = SCHEMA_SQL.read_text(encoding="utf-8")
        with self._pool.connection() as conn:
            conn.execute(sql)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(self._ensure_schema_sync)

    PARTITIONED = ("telemetry", "topic_samples")

    def _ensure_partitions_sync(self, days_ahead: int) -> List[str]:
        created = []
        today = datetime.now(timezone.utc).date()
        with self._pool.connection() as conn:
            for offset in range(days_ahead + 1):
                day = today + timedelta(days=offset)
                for parent in self.PARTITIONED:
                    name = f"{parent}_{day:%Y%m%d}"
                    conn.execute(f"""
                        CREATE TABLE IF NOT EXISTS {name}
                        PARTITION OF {parent}
                        FOR VALUES FROM ('{day:%Y-%m-%d}') TO ('{day + timedelta(days=1):%Y-%m-%d}')
                    """)
                    created.append(name)
        return created

    async def ensure_partitions(self, days_ahead: Optional[int] = None) -> List[str]:
        """Create today's and the next days' partitions ahead of time, so the
        writer never trips over a missing partition at midnight."""
        if days_ahead is None:
            days_ahead = self.partition_days_ahead
        return await asyncio.to_thread(self._ensure_partitions_sync, days_ahead)

    # ------------------------------------------------------------ disk guard

    def _data_dir_sync(self) -> Optional[str]:
        with self._pool.connection() as conn:
            row = conn.execute("SHOW data_directory").fetchone()
        return row[0] if row else None

    def _disk_sync(self) -> Dict[str, Any]:
        """Free space on whatever volume PostgreSQL actually writes to.

        Asking the database rather than assuming: the data directory is often
        on a different volume from the application, and guarding the wrong one
        is the same as not guarding at all.
        """
        path = self._data_dir_sync()
        target = path if path and os.path.isdir(path) else os.getcwd()
        usage = shutil.disk_usage(target)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT pg_database_size(current_database())").fetchone()
        return {
            "path": target,
            "total": usage.total,
            "free": usage.free,
            "used": usage.used,
            "free_pct": round(usage.free / usage.total * 100, 1) if usage.total else 0.0,
            "db_bytes": int(row[0]) if row else 0,
        }

    async def disk(self) -> Dict[str, Any]:
        if not self.ready:
            return {}
        try:
            return await asyncio.to_thread(self._disk_sync)
        except Exception as exc:
            log.warning("disk check failed: %s", exc)
            return {}

    def _oldest_partition_sync(self) -> Optional[str]:
        """The oldest daily partition, or None if only the default remains."""
        oldest = None
        with self._pool.connection() as conn:
            for parent in self.PARTITIONED:
                rows = conn.execute("""
                    SELECT c.relname FROM pg_class c
                    JOIN pg_inherits i ON i.inhrelid = c.oid
                    JOIN pg_class p ON p.oid = i.inhparent
                    WHERE p.relname = %s AND c.relname ~ '_[0-9]{8}$'
                    ORDER BY c.relname LIMIT 1
                """, (parent,)).fetchall()
                if rows:
                    day = rows[0][0][-8:]
                    if oldest is None or day < oldest:
                        oldest = day
        return oldest

    def _drop_day_sync(self, day: str) -> List[str]:
        dropped = []
        with self._pool.connection() as conn:
            for parent in self.PARTITIONED:
                name = f"{parent}_{day}"
                conn.execute(f"DROP TABLE IF EXISTS {name}")
                dropped.append(name)
        return dropped

    async def emergency_drop_oldest_day(self) -> List[str]:
        """Shed one day of history to buy time. Last resort, and it is lossy.

        Losing the oldest day is bad. A full disk is worse: PostgreSQL stops
        accepting writes, and on the same volume as the OS it can take the
        whole machine down. Between "lose the oldest day" and "lose the ability
        to record anything at all", this picks the first -- and logs an event
        so the loss is never silent.
        """
        day = await asyncio.to_thread(self._oldest_partition_sync)
        if not day:
            return []
        return await asyncio.to_thread(self._drop_day_sync, day)

    def _drop_expired_sync(self) -> List[str]:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=self.retention_days)
        dropped = []
        with self._pool.connection() as conn:
            rows = conn.execute("""
                SELECT c.relname, p.relname
                FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname = ANY(%s) AND c.relname ~ '_[0-9]{8}$'
            """, (list(self.PARTITIONED),)).fetchall()
            for name, parent in rows:
                stamp = name[len(parent) + 1:]
                try:
                    part_day = date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
                except ValueError:
                    continue
                if part_day < cutoff:
                    conn.execute(f"DROP TABLE IF EXISTS {name}")
                    dropped.append(name)

            # The DEFAULT partition catches anything outside every daily
            # range, and the regex above deliberately skips it -- dropping it
            # would break inserts. But that means rows landing there were
            # exempt from retention forever. `ts` is now clamped so this
            # should stay empty; sweep it anyway, because "should stay empty"
            # is exactly the assumption that quietly stops being true.
            for parent in self.PARTITIONED:
                n = conn.execute(
                    f"DELETE FROM {parent}_default WHERE ts < %s", (cutoff,)
                ).rowcount
                if n:
                    dropped.append(f"{parent}_default({n} 列)")
        return dropped

    async def drop_expired_partitions(self) -> List[str]:
        """Retention by DROP TABLE -- instant, and leaves no dead tuples behind."""
        dropped = await asyncio.to_thread(self._drop_expired_sync)
        if dropped:
            log.info("dropped %d expired partitions: %s", len(dropped), ", ".join(dropped))
        return dropped

    # ----------------------------------------------------------------- write

    def _copy_sync(self, rows: Sequence[tuple]) -> int:
        cols = ", ".join(TELEMETRY_COLS)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                with cur.copy(f"COPY telemetry ({cols}) FROM STDIN (FORMAT BINARY)") as cp:
                    cp.set_types(TELEMETRY_TYPES)
                    for row in rows:
                        cp.write_row(row)
        return len(rows)

    async def copy_telemetry(self, rows: Sequence[tuple]) -> int:
        """Bulk-load a batch. COPY rather than INSERT: far less CPU per row."""
        if not rows:
            return 0
        return await asyncio.to_thread(self._copy_sync, rows)

    def _copy_topics_sync(self, rows: Sequence[tuple]) -> int:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                with cur.copy("COPY topic_samples"
                              " (robot_id, ts, recv_ts, skew_ms,"
                              "  topic, num, payload, flag)"
                              " FROM STDIN (FORMAT BINARY)") as cp:
                    cp.set_types(["text", "timestamptz", "timestamptz", "int4",
                                  "text", "real", "jsonb", "int2"])
                    for row in rows:
                        cp.write_row(row)
        return len(rows)

    async def copy_topic_samples(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        return await asyncio.to_thread(self._copy_topics_sync, rows)

    def _catalog_sync(self, rows: Sequence[tuple]) -> int:
        """Upsert the per-robot topic catalogue that the admin UI browses."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO topic_catalog
                        (robot_id, topic, samples, last_value,
                         last_seen, last_changed)
                    VALUES (%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))
                    ON CONFLICT (robot_id, topic) DO UPDATE
                    SET samples    = topic_catalog.samples + EXCLUDED.samples,
                        last_value = EXCLUDED.last_value,
                        last_seen  = EXCLUDED.last_seen,
                        -- only advance last_changed when this flush actually
                        -- contained a change; a flush of pure heartbeats has
                        -- 0 here and must leave the old value alone
                        last_changed = GREATEST(
                            topic_catalog.last_changed,
                            NULLIF(EXCLUDED.last_changed, to_timestamp(0)))
                """, rows)
        return len(rows)

    async def upsert_catalog(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        return await asyncio.to_thread(self._catalog_sync, rows)

    def _events_sync(self, rows: Sequence[tuple]) -> int:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # events are the audit trail, so they must survive a power cut
                cur.execute("SET LOCAL synchronous_commit = on")
                cur.executemany(
                    "INSERT INTO events (robot_id, ts, kind, severity, detail)"
                    " VALUES (%s, %s, %s, %s, %s)", rows)
        return len(rows)

    async def insert_events(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        return await asyncio.to_thread(self._events_sync, rows)

    def _touch_sync(self, robot_ids: Sequence[str]) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE robots SET last_seen = now() WHERE id = ANY(%s)",
                         (list(robot_ids),))

    async def touch_last_seen(self, robot_ids: Sequence[str]) -> None:
        if robot_ids:
            await asyncio.to_thread(self._touch_sync, robot_ids)

    # ------------------------------------------------------------------ read

    def _fetch_sync(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
        with self._pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return cur.fetchall()

    async def fetch(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """For statements that return rows. Use execute() for ones that do not."""
        return await asyncio.to_thread(self._fetch_sync, sql, params)

    def _execute_sync(self, sql: str, params: Sequence[Any]) -> int:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """INSERT/UPDATE/DELETE without RETURNING. Returns affected row count."""
        return await asyncio.to_thread(self._execute_sync, sql, params)

    async def storage_stats(self) -> Dict[str, Any]:
        """Real on-disk numbers, so retention sizing is measured not guessed."""
        rows = await self.fetch("""
            SELECT c.relname                     AS part,
                   c.reltuples::bigint           AS approx_rows,
                   pg_total_relation_size(c.oid) AS bytes
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class p ON p.oid = i.inhparent
            WHERE p.relname = 'telemetry'
            ORDER BY c.relname
        """)
        total_bytes = sum(r["bytes"] for r in rows)
        n = (await self.fetch("SELECT count(*) AS n FROM telemetry"))[0]["n"] or 0
        db_size = (await self.fetch(
            "SELECT pg_database_size(current_database()) AS bytes"))[0]["bytes"]
        return {
            "partitions": rows,
            "partition_count": len(rows),
            "telemetry_rows": n,
            "telemetry_bytes": total_bytes,
            "bytes_per_row": round(total_bytes / n, 1) if n else None,
            "database_bytes": db_size,
        }
