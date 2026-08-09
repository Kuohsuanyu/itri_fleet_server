"""Live topic scanner.

Connects to the vehicle's own broker, subscribes to everything, and redraws a
numbered table as messages arrive. You then pick numbers -- `1,3,5-8` -- and the
selection is written to the config.

This is what removes per-chassis engineering: you never need to know what a new
chassis publishes, you watch it publish and tick the boxes.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Set

import paho.mqtt.client as mqtt

from .config import should_relay

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"


class TopicStat:
    __slots__ = ("topic", "count", "last_value", "last_at", "first_at",
                 "bytes_total", "changed", "binary")

    def __init__(self, topic: str):
        self.topic = topic
        self.count = 0
        self.last_value: Any = None
        self.last_at = 0.0
        self.first_at = time.time()
        self.bytes_total = 0
        self.changed = 0
        self.binary = False

    @property
    def hz(self) -> float:
        span = max(self.last_at - self.first_at, 1e-6)
        return (self.count - 1) / span if self.count > 1 else 0.0

    @property
    def avg_bytes(self) -> float:
        return self.bytes_total / self.count if self.count else 0.0


def decode(payload: bytes) -> tuple[Any, bool]:
    """Return (value, is_binary). Numbers and JSON come back as real types."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(payload)} bytes binary>", True
    text = text.strip()
    if not text:
        return "", False
    try:
        return json.loads(text), False
    except (json.JSONDecodeError, ValueError):
        return text, False


def _fmt_value(v: Any, width: int) -> str:
    if isinstance(v, float):
        s = f"{v:.4g}"
    elif isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    else:
        s = str(v)
    s = s.replace("\n", " ")
    return s if len(s) <= width else s[: width - 1] + "…"


def scan(host: str, port: int, seconds: float = 0.0,
         username: Optional[str] = None, password: Optional[str] = None,
         subscribe: Optional[List[str]] = None,
         exclude: Optional[List[str]] = None,
         refresh: float = 0.5) -> Dict[str, TopicStat]:
    """Watch the local broker and redraw a live table until Ctrl-C or timeout."""
    stats: Dict[str, TopicStat] = {}
    recent: Set[str] = set()
    exclude = exclude or []
    subscribe = subscribe or ["#"]
    connected = {"ok": False, "rc": None}

    def on_connect(c, u, flags, rc, props=None):
        connected["rc"] = int(getattr(rc, "value", rc))
        connected["ok"] = connected["rc"] == 0
        if connected["ok"]:
            for f in subscribe:
                c.subscribe(f, qos=0)

    def on_message(c, u, msg):
        if not should_relay(msg.topic, [], exclude):
            return
        st = stats.get(msg.topic)
        if st is None:
            st = stats[msg.topic] = TopicStat(msg.topic)
        value, is_bin = decode(msg.payload)
        if st.count and value != st.last_value:
            st.changed += 1
        st.count += 1
        st.bytes_total += len(msg.payload)
        st.last_value = value
        st.last_at = time.time()
        st.binary = st.binary or is_bin
        recent.add(msg.topic)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="itri-agent-discover")
    if username:
        client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()

    t0 = time.time()
    while not connected["rc"] and time.time() - t0 < 8:
        time.sleep(0.05)
    if not connected["ok"]:
        client.loop_stop()
        raise ConnectionError(
            f"local broker {host}:{port} refused the connection "
            f"(CONNACK {connected['rc']})")

    print(f"{DIM}連上本地 broker {host}:{port},訂閱 {', '.join(subscribe)}{RESET}")
    print(f"{DIM}Ctrl-C 結束掃描{RESET}\n")
    lines_drawn = 0
    try:
        while True:
            time.sleep(refresh)
            lines_drawn = _draw(stats, recent, t0, lines_drawn)
            recent.clear()
            if seconds and time.time() - t0 >= seconds:
                break
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
    print()
    return stats


def _draw(stats: Dict[str, TopicStat], recent: Set[str],
          t0: float, previous_lines: int) -> int:
    """Redraw in place. Topics that ticked since the last frame are highlighted."""
    if previous_lines:
        sys.stdout.write(f"\x1b[{previous_lines}A")

    width = shutil.get_terminal_size((100, 30)).columns
    topic_w = max(24, min(46, width - 52))
    val_w = max(14, width - topic_w - 40)

    out = [f"{BOLD}{'  #':>4}  {'TOPIC':<{topic_w}} {'Hz':>6} {'筆數':>7} "
           f"{'位元組':>7}  {'最新值':<{val_w}}{RESET}",
           DIM + "─" * min(width - 1, topic_w + val_w + 32) + RESET]

    for i, topic in enumerate(sorted(stats), start=1):
        st = stats[topic]
        live = topic in recent
        mark = f"{GREEN}●{RESET}" if live else " "
        colour = CYAN if live else ""
        name = topic if len(topic) <= topic_w else "…" + topic[-(topic_w - 1):]
        flag = f"{YELLOW}bin{RESET}" if st.binary else ""
        out.append(
            f"{mark}{i:>3}  {colour}{name:<{topic_w}}{RESET} "
            f"{st.hz:>6.1f} {st.count:>7} {st.avg_bytes:>7.0f}  "
            f"{_fmt_value(st.last_value, val_w):<{val_w}}{flag}")

    elapsed = time.time() - t0
    total = sum(s.count for s in stats.values())
    out.append(f"{DIM}{len(stats)} 個 topic · {total} 筆 · "
               f"{total/max(elapsed,1):.1f} 筆/秒 · 已掃描 {elapsed:.0f}s{RESET}")

    for line in out:
        sys.stdout.write("\x1b[2K" + line + "\n")
    sys.stdout.flush()
    return len(out)


def parse_selection(text: str, count: int) -> List[int]:
    """`1,3,5-8` or `all` -> a list of 1-based indices."""
    text = text.strip().lower()
    if text in ("all", "*", "全部"):
        return list(range(1, count + 1))
    if not text:
        return []
    picked: Set[int] = set()
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                for n in range(int(lo), int(hi) + 1):
                    picked.add(n)
            except ValueError:
                raise ValueError(f"看不懂的範圍:{chunk!r}")
        else:
            try:
                picked.add(int(chunk))
            except ValueError:
                raise ValueError(f"看不懂的編號:{chunk!r}")
    bad = [n for n in picked if not 1 <= n <= count]
    if bad:
        raise ValueError(f"編號超出範圍 1-{count}: {sorted(bad)}")
    return sorted(picked)


def estimate_cost(stats: Dict[str, TopicStat], topics: List[str],
                  max_rate_hz: float, on_change_only: bool) -> Dict[str, float]:
    """What the selection will cost the server, before committing to it."""
    rows_per_s = 0.0
    for t in topics:
        st = stats.get(t)
        if not st:
            continue
        rate = min(st.hz, max_rate_hz)
        if on_change_only and st.count > 1:
            change_ratio = st.changed / max(st.count - 1, 1)
            rate *= max(change_ratio, 0.0)
        rows_per_s += rate
    bytes_per_row = 170.0     # measured on this schema, including indexes
    return {
        "rows_per_s": rows_per_s,
        "gb_per_day": rows_per_s * bytes_per_row * 86400 / 1e9,
        "gb_per_month": rows_per_s * bytes_per_row * 86400 * 30 / 1e9,
    }
