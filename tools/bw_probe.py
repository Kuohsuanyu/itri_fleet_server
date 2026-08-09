"""Measure what Tailscale Funnel actually gives you.

Tailscale documents that Funnel is "subject to non-configurable bandwidth
limits" but never says what they are, and there is no usage meter in the admin
console.  So measure it:

  python tools/bw_probe.py https://<node>.<tailnet>.ts.net --all

  --throughput  how fast a bulk download goes through the Funnel relay
  --dashboard   what one open dashboard tab really costs per hour/day/month
  --soak        sustained transfer, to see whether a cap kicks in over time

Compare each result against the same test on the direct tailnet address
(http://100.x.y.z:8080) to separate "Funnel is throttling" from
"my uplink is slow".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import httpx

try:
    import websockets
except ImportError:
    websockets = None


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def rate(bps: float) -> str:
    return f"{human(bps)}/s  ({bps * 8 / 1e6:.2f} Mbit/s)"


def with_token(url: str, token: Optional[str]) -> str:
    if not token:
        return url
    p = urlparse(url)
    q = f"token={token}" if not p.query else p.query + f"&token={token}"
    return urlunparse(p._replace(query=q))


# ------------------------------------------------------------------ probes

def probe_throughput(base: str, token: Optional[str], sizes: List[int], repeats: int) -> dict:
    """Download increasingly large blobs; report the best sustained rate."""
    print("\n=== 1. 吞吐量 (單一連線批次下載) ===")
    results = {}
    with httpx.Client(timeout=180.0, follow_redirects=True, verify=True) as client:
        for size in sizes:
            samples = []
            for _ in range(repeats):
                url = with_token(f"{base}/api/bwtest?bytes={size}", token)
                t0 = time.perf_counter()
                got = 0
                try:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        for chunk in resp.iter_bytes(65536):
                            got += len(chunk)
                except Exception as exc:
                    print(f"  {human(size):>10}  FAILED: {type(exc).__name__}: {exc}")
                    break
                dt = time.perf_counter() - t0
                samples.append(got / dt)
            if samples:
                best = max(samples)
                results[size] = best
                print(f"  {human(size):>10}  best {rate(best):<34}"
                      f" median {rate(statistics.median(samples))}")
    if results:
        peak = max(results.values())
        print(f"\n  峰值吞吐量: {rate(peak)}")
        results["peak_bps"] = peak
    return results


def probe_parallel(base: str, token: Optional[str], streams: int, size: int) -> dict:
    """Aggregate rate across N concurrent downloads -- reveals per-connection caps."""
    print(f"\n=== 2. 併發吞吐量 ({streams} 條連線 x {human(size)}) ===")
    import concurrent.futures as cf

    def one(_i: int) -> int:
        url = with_token(f"{base}/api/bwtest?bytes={size}", token)
        got = 0
        with httpx.Client(timeout=180.0) as c, c.stream("GET", url) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes(65536):
                got += len(chunk)
        return got

    t0 = time.perf_counter()
    total = 0
    with cf.ThreadPoolExecutor(max_workers=streams) as pool:
        for fut in cf.as_completed([pool.submit(one, i) for i in range(streams)]):
            try:
                total += fut.result()
            except Exception as exc:
                print(f"  stream failed: {type(exc).__name__}: {exc}")
    dt = time.perf_counter() - t0
    agg = total / dt
    print(f"  合計 {human(total)} / {dt:.2f}s  =  {rate(agg)}")
    return {"parallel_bps": agg, "streams": streams}


async def probe_dashboard(base: str, token: Optional[str], seconds: int) -> dict:
    """Sit on the WebSocket like a real browser tab and count every byte."""
    print(f"\n=== 3. 儀表板實際用量 (掛著 {seconds}s 不動) ===")
    if websockets is None:
        print("  略過: pip install websockets")
        return {}
    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    ws_url = with_token(ws_url, token)

    total = frames = 0
    t0 = time.perf_counter()
    try:
        async with websockets.connect(ws_url, max_size=8 << 20) as ws:
            while time.perf_counter() - t0 < seconds:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=seconds)
                except asyncio.TimeoutError:
                    break
                total += len(msg)
                frames += 1
    except Exception as exc:
        print(f"  WebSocket 失敗: {type(exc).__name__}: {exc}")
        return {}

    dt = time.perf_counter() - t0
    bps = total / dt
    print(f"  {frames} frames / {human(total)} in {dt:.1f}s  =  {rate(bps)}")
    print(f"  一個分頁掛著:  {human(bps*3600):>10}/小時   "
          f"{bps*86400/1e9:.3f} GB/天   {bps*86400*30/1e9:.2f} GB/月")
    print(f"  平均 frame 大小 {total/max(frames,1):.0f} B")
    return {"ws_bps": bps, "frames": frames, "gb_month_per_tab": bps * 86400 * 30 / 1e9}


def probe_soak(base: str, token: Optional[str], seconds: int, chunk_mb: int) -> dict:
    """Hammer it continuously -- a cap that only appears under sustained load
    shows up here as a rate that decays after the first N seconds."""
    print(f"\n=== 4. 持續負載 ({seconds}s,看是否被限速) ===")
    url = with_token(f"{base}/api/bwtest?bytes={chunk_mb * 1_000_000}", token)
    buckets: List[float] = []
    total = 0
    t0 = time.perf_counter()
    bucket_start, bucket_bytes = t0, 0
    try:
        with httpx.Client(timeout=300.0) as client:
            while time.perf_counter() - t0 < seconds:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    for chunk in resp.iter_bytes(65536):
                        n = len(chunk)
                        total += n
                        bucket_bytes += n
                        now = time.perf_counter()
                        if now - bucket_start >= 5.0:
                            buckets.append(bucket_bytes / (now - bucket_start))
                            print(f"  t+{now-t0:5.0f}s   {rate(buckets[-1])}")
                            bucket_start, bucket_bytes = now, 0
                        if now - t0 >= seconds:
                            break
    except Exception as exc:
        print(f"  中斷: {type(exc).__name__}: {exc}")

    dt = time.perf_counter() - t0
    print(f"\n  傳輸總量 {human(total)} / {dt:.0f}s  平均 {rate(total/dt)}")
    verdict = "inconclusive"
    if len(buckets) >= 4:
        head = statistics.mean(buckets[:2])
        tail = statistics.mean(buckets[-2:])
        drop = (1 - tail / head) * 100 if head else 0.0
        print(f"  前段 {rate(head)}  ->  後段 {rate(tail)}")
        if drop > 25:
            verdict = "throttled"
            print(f"  [!] 後段掉了 {drop:.0f}%,有明顯限速跡象")
        elif drop < -10:
            verdict = "no-throttle"
            print(f"  [OK] 後段反而快了 {-drop:.0f}%(TCP 慢啟動),未觀察到節流")
        else:
            verdict = "no-throttle"
            print(f"  [OK] 速率穩定(變化 {-drop:+.0f}%),未觀察到節流")
    return {"soak_avg_bps": total / dt, "soak_total_bytes": total,
            "buckets": buckets, "verdict": verdict}


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Tailscale Funnel bandwidth probe")
    ap.add_argument("base", help="e.g. https://<node>.<tailnet>.ts.net or http://127.0.0.1:8080")
    ap.add_argument("--token", help="dashboard token if config.yaml sets one")
    ap.add_argument("--all", action="store_true", help="run every probe")
    ap.add_argument("--throughput", action="store_true")
    ap.add_argument("--parallel", action="store_true")
    ap.add_argument("--dashboard", action="store_true")
    ap.add_argument("--soak", action="store_true")
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--soak-seconds", type=int, default=120)
    ap.add_argument("--streams", type=int, default=6)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--json", help="write raw results here")
    args = ap.parse_args()

    # Windows consoles default to cp950 here and choke on the report glyphs
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    base = args.base.rstrip("/")
    if args.all:
        args.throughput = args.parallel = args.dashboard = args.soak = True
    if not any([args.throughput, args.parallel, args.dashboard, args.soak]):
        args.throughput = args.dashboard = True

    print(f"target: {base}")
    try:
        r = httpx.get(with_token(f"{base}/healthz", args.token), timeout=20.0)
        print(f"health: {r.status_code} {r.text.strip()!r}")
    except Exception as exc:
        print(f"health check failed: {type(exc).__name__}: {exc}")
        return 1

    out = {"target": base, "ts": time.time()}
    if args.throughput:
        out["throughput"] = probe_throughput(
            base, args.token, [1_000_000, 8_000_000, 32_000_000], args.repeats)
    if args.parallel:
        out["parallel"] = probe_parallel(base, args.token, args.streams, 8_000_000)
    if args.dashboard:
        out["dashboard"] = asyncio.run(probe_dashboard(base, args.token, args.seconds))
    if args.soak:
        out["soak"] = probe_soak(base, args.token, args.soak_seconds, 32)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nraw results -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
