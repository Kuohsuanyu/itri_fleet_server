"""Egress accounting.

Tailscale does not publish a Funnel bandwidth number and gives you no usage
meter, so the only way to know where you stand is to measure your own egress
and project it.  Every byte this server sends to a browser -- HTTP bodies and
WebSocket frames -- is counted here and projected to a monthly figure.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, Tuple

WINDOW_SECONDS = 60.0


class EgressMeter:
    def __init__(self) -> None:
        self.started = time.time()
        self.http_bytes = 0
        self.ws_bytes = 0
        self.http_requests = 0
        self.ws_frames = 0
        self.peak_bps = 0.0
        self._window: Deque[Tuple[float, int]] = deque()

    def _record(self, n: int) -> None:
        now = time.monotonic()
        self._window.append((now, n))
        cutoff = now - WINDOW_SECONDS
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        self.peak_bps = max(self.peak_bps, self.rate_bps())

    def add_http(self, n: int) -> None:
        self.http_bytes += n
        self.http_requests += 1
        self._record(n)

    def add_ws(self, n: int) -> None:
        self.ws_bytes += n
        self.ws_frames += 1
        self._record(n)

    def rate_bps(self) -> float:
        """Bytes/second averaged over the trailing window."""
        if not self._window:
            return 0.0
        span = max(time.monotonic() - self._window[0][0], 1.0)
        return sum(n for _, n in self._window) / span

    def snapshot(self, ws_clients: int = 0) -> Dict[str, Any]:
        total = self.http_bytes + self.ws_bytes
        uptime = max(time.time() - self.started, 1.0)
        rate = self.rate_bps()
        return {
            "uptime_s": round(uptime, 1),
            "http_bytes": self.http_bytes,
            "ws_bytes": self.ws_bytes,
            "total_bytes": total,
            "http_requests": self.http_requests,
            "ws_frames": self.ws_frames,
            "ws_clients": ws_clients,
            "rate_bps": round(rate, 1),
            "rate_kbps": round(rate * 8 / 1000, 1),
            "peak_bps": round(self.peak_bps, 1),
            "avg_bps_session": round(total / uptime, 1),
            # what this costs you if the current rate keeps up
            "projected_gb_day": round(rate * 86400 / 1e9, 3),
            "projected_gb_month": round(rate * 86400 * 30 / 1e9, 2),
            # what it costs based on everything sent since boot
            "actual_gb_month_at_session_avg": round(total / uptime * 86400 * 30 / 1e9, 2),
        }
