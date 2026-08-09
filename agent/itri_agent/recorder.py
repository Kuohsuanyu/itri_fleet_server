"""Full-rate local recording on the vehicle.

The uplink is deliberately lossy: it throttles to `max_rate_hz`, drops repeats
when `on_change_only` is set, and caps payload size. That is the right trade
for a fleet dashboard -- the server's disk pays for every sample from every
robot -- but it means the archive on the server is a *summary*, not the raw
signal. When something goes wrong at 50 Hz, a 5 Hz summary can be exactly the
part that hides the cause.

This module keeps the unfiltered stream on the vehicle's own disk, where it
costs nothing to anyone else. It is written *before* the uplink filters run, so
it is genuinely everything the source produced.

Format is gzipped JSON Lines, one sample per line:

    {"t": 1765240151.482, "n": "/odom/twist/twist/linear/x", "v": 0.62}

JSONL rather than a columnar format on purpose: a partially written file is
still readable up to the last complete line, which is what you have after a
power cut. That property matters more here than compression ratio, and gzip
already gets the file most of the way down.

Disk is bounded two ways -- rotate at `rotate_mb`, and delete oldest files once
the directory exceeds `max_gb`. A recorder that fills the vehicle's SD card is
worse than no recorder, because it takes the robot down with it.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("recorder")

SUFFIX = ".jsonl.gz"


class Recorder:
    def __init__(self, directory: str, rate_hz: float = 0.0,
                 rotate_mb: float = 64.0, max_gb: float = 5.0,
                 compress: bool = True):
        """rate_hz = 0 means no sampling at all -- record every sample.

        Any other value throttles per topic, exactly like the uplink does, for
        people who want a middle ground between "everything" and "the uplink
        summary".
        """
        self.dir = Path(os.path.expanduser(directory))
        self.rate_hz = max(float(rate_hz), 0.0)
        self.rotate_bytes = int(max(rotate_mb, 1.0) * 1024 * 1024)
        self.max_bytes = int(max(max_gb, 0.1) * 1024 * 1024 * 1024)
        self.compress = bool(compress)

        self._lock = threading.Lock()
        self._fh = None
        self._path: Optional[Path] = None
        self._written = 0          # bytes into the current file, uncompressed
        self._last_at: Dict[str, float] = {}
        self._disabled = False     # set after a write error; uplink continues

        self.samples = 0
        self.skipped_rate = 0
        self.files_rotated = 0
        self.files_deleted = 0
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ write

    def write(self, topic: str, ts: float, value: Any) -> None:
        if self._disabled:
            return
        if self.rate_hz:
            gap = 1.0 / self.rate_hz
            if ts - self._last_at.get(topic, 0.0) < gap:
                self.skipped_rate += 1
                return
            self._last_at[topic] = ts

        line = json.dumps({"t": round(ts, 4), "n": topic, "v": value},
                          separators=(",", ":"), ensure_ascii=False,
                          default=str) + "\n"
        data = line.encode("utf-8")

        with self._lock:
            try:
                if self._fh is None:
                    self._open()
                self._fh.write(data)
                self._written += len(data)
                self.samples += 1
                if self._written >= self.rotate_bytes:
                    self._rotate()
            except OSError as exc:
                # A full or read-only disk must not take the uplink down with
                # it: stop recording, keep relaying.
                self.last_error = str(exc)
                log.error("recording stopped (uplink continues): %s", exc)
                self._close()
                self._disabled = True

    # ------------------------------------------------------------------ files

    def _open(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        # Sortable name, no colons -- this has to survive being copied to a
        # Windows machine for analysis.
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        suffix = SUFFIX if self.compress else ".jsonl"
        self._path = self.dir / f"{stamp}{suffix}"
        n = 1
        while self._path.exists():          # same-second restart
            self._path = self.dir / f"{stamp}_{n}{suffix}"
            n += 1
        self._fh = (gzip.open(self._path, "ab") if self.compress
                    else open(self._path, "ab"))
        self._written = 0

    def _rotate(self) -> None:
        self._close()
        self.files_rotated += 1
        self._prune()

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def _prune(self) -> None:
        """Delete oldest files until the directory is back under the cap."""
        files = sorted(self.dir.glob(f"*{SUFFIX}")) + sorted(self.dir.glob("*.jsonl"))
        files = sorted(set(files), key=lambda p: p.name)
        total = 0
        sizes: List[tuple] = []
        for p in files:
            try:
                s = p.stat().st_size
            except OSError:
                continue
            sizes.append((p, s))
            total += s
        i = 0
        while total > self.max_bytes and i < len(sizes):
            p, s = sizes[i]
            try:
                p.unlink()
                total -= s
                self.files_deleted += 1
                log.info("recording pruned: %s", p.name)
            except OSError:
                pass
            i += 1

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except OSError:
                    pass

    def stop(self) -> None:
        with self._lock:
            self._close()

    # ------------------------------------------------------------------ status

    def disk_used(self) -> int:
        total = 0
        if not self.dir.exists():
            return 0
        for p in self.dir.iterdir():
            if p.suffix in (".gz", ".jsonl"):
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def stats(self) -> Dict[str, Any]:
        return {
            "dir": str(self.dir),
            "rate_hz": self.rate_hz or "全速(不取樣)",
            "samples": self.samples,
            "skipped_rate": self.skipped_rate,
            "rotated": self.files_rotated,
            "deleted": self.files_deleted,
            "disk_mb": round(self.disk_used() / 1e6, 1),
            "cap_gb": round(self.max_bytes / 1e9, 1),
            "current": self._path.name if self._path else None,
            "error": self.last_error,
        }


def read_recording(path: str):
    """Iterate one recording. Tolerates a truncated final line."""
    p = Path(path)
    opener = gzip.open if p.suffix == ".gz" else open
    with opener(p, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                return          # truncated tail after a power cut; stop cleanly
