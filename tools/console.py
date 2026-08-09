"""ITRI Fleet 控制台 —— 一個畫面看完所有元件狀態,並可以開關。

Windows 上沒有 systemctl,而這套東西由五個獨立的行程組成(PostgreSQL、
fleet server、Funnel、模擬底盤、agent),散在不同視窗裡很容易搞不清楚
誰活著、誰重複開了兩份。這支就是那個缺席的控制面板。

    python tools/console.py          互動選單
    python tools/console.py --status 印一次狀態就結束(給排程/腳本用)
    python tools/console.py --watch  持續刷新
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import unicodedata
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def _config() -> dict:
    """Read config.yaml so nothing here is hardcoded per machine."""
    try:
        import yaml
        return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


CFG = _config()
PGBIN = Path(os.environ.get("ITRI_PGBIN") or Path.home() / "pgsql" / "bin")
PGDATA = Path(os.environ.get("ITRI_PGDATA") or Path.home() / "pgdata-itri")
TAILSCALE = Path(os.environ.get("ITRI_TAILSCALE")
                 or r"C:\Program Files\Tailscale\tailscale.exe")
PORT = int(CFG.get("http", {}).get("port", 8080))
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = os.environ.get("FLEET_PASSWORD") or CFG.get("http", {}).get("password") or ""


def _public_url() -> str:
    """The Funnel URL is this node's MagicDNS name. Ask Tailscale rather than
    baking it in, so the console still works after moving to new hardware."""
    try:
        out = subprocess.run([str(TAILSCALE), "status", "--json"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        name = (json.loads(out)["Self"]["DNSName"] or "").rstrip(".")
        if name:
            return f"https://{name}"
    except Exception:
        pass
    return ""


PUBLIC = _public_url()

R = "\x1b[0m"; B = "\x1b[1m"; D = "\x1b[2m"
GRN = "\x1b[32m"; RED = "\x1b[31m"; YEL = "\x1b[33m"; CYA = "\x1b[36m"

# label -> substring that identifies the process on its command line
SERVICES = {
    "server":  "server.main",
    "chassis": "sim_chassis",
    "fleet":   "sim_robots",
    "agent":   "itri_agent",
}


# --------------------------------------------------------------- inspection

def _procs() -> List[Tuple[int, str]]:
    """(pid, commandline) for every python.exe. wmic CSV, parsed properly --
    grepping its output is what produced phantom matches before."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    rows = []
    for row in csv.reader(io.StringIO(out.strip())):
        if len(row) < 3 or row[0] == "Node" or not row[-1].strip().isdigit():
            continue
        rows.append((int(row[-1]), ",".join(row[1:-1])))
    return rows


def find(marker: str) -> List[int]:
    return [pid for pid, cmd in _procs() if marker in cmd]


def port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def http(url: str, timeout: float = 6.0, auth: bool = False) -> Tuple[Optional[int], Any]:
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", f"Bearer {PASSWORD}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def pg_running() -> bool:
    return port_open("127.0.0.1", 5432)


def funnel_state() -> str:
    if not TAILSCALE.exists():
        return "未安裝"
    try:
        out = subprocess.run([str(TAILSCALE), "funnel", "status"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return "查詢失敗"
    return "on" if "Funnel on" in out else "off"


def collect() -> Dict[str, Any]:
    s: Dict[str, Any] = {}
    s["pg"] = pg_running()
    s["server_pids"] = find(SERVICES["server"])
    s["chassis_pids"] = find(SERVICES["chassis"])
    s["fleetsim_pids"] = find(SERVICES["fleet"])
    s["agent_pids"] = find(SERVICES["agent"])
    s["http"] = port_open("127.0.0.1", 8080)
    bind = str(CFG.get("mqtt", {}).get("bind", "0.0.0.0"))
    probe = "127.0.0.1" if bind in ("0.0.0.0", "tailscale", "") else bind
    s["mqtt"] = port_open(probe, 1883) or port_open("127.0.0.1", 1883)
    s["funnel"] = funnel_state()

    code, _ = http(f"{BASE}/healthz", 3)
    s["local_ok"] = code == 200
    s["public_code"] = http(f"{PUBLIC}/healthz", 12)[0] if PUBLIC else None

    s["metrics"] = None
    if s["local_ok"]:
        code, data = http(f"{BASE}/api/metrics", 6, auth=True)
        if isinstance(data, dict):
            s["metrics"] = data
        code, data = http(f"{BASE}/api/alerts/active", 6, auth=True)
        if isinstance(data, dict):
            s["alerts"] = data
    return s


# ------------------------------------------------------------------ display

ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def width(text: str) -> int:
    """Display columns. CJK glyphs occupy two, ANSI colour codes occupy none --
    plain str.ljust gets both wrong and the table comes out ragged."""
    bare = ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in bare)


def pad(text: str, cols: int) -> str:
    return text + " " * max(cols - width(text), 0)


def badge(ok: Optional[bool], yes="執行中", no="停止", unknown="?") -> str:
    if ok is None:
        return f"{YEL}{unknown}{R}"
    return f"{GRN}● {yes}{R}" if ok else f"{D}○ {no}{R}"


def render(s: Dict[str, Any]) -> str:
    L: List[str] = []
    add = L.append
    add(f"{B}{CYA}  ITRI Fleet 控制台{R}   {D}{time.strftime('%Y-%m-%d %H:%M:%S')}{R}")
    add(f"{D}  {'─' * 68}{R}")

    def row(name: str, state: str, detail: str = "") -> None:
        add(f"   {pad(name, 18)}{pad(state, 16)} {D}{detail}{R}")

    row("PostgreSQL", badge(s["pg"]), "127.0.0.1:5432" if s["pg"] else "資料無法寫入")

    n = len(s["server_pids"])
    dup = f"{RED}⚠ 有 {n} 份!{R}" if n > 1 else ""
    row("Fleet Server", badge(bool(n)),
        f"PID {s['server_pids'] or '-'}  :8080 {'開' if s['http'] else '關'} {dup}")

    bind = CFG.get("mqtt", {}).get("bind", "0.0.0.0")
    row("MQTT broker", badge(s["mqtt"]), f"port 1883 · bind={bind}"
        + ("(只聽 Tailscale 介面)" if bind != "0.0.0.0" else "(所有介面)"))

    f = s["funnel"]
    row("Tailscale Funnel", badge(f == "on", "已發佈", "未發佈") if f in ("on", "off")
        else f"{YEL}{f}{R}", PUBLIC or "(Tailscale 未登入)")

    pc = s["public_code"]
    row("外網可達", badge(pc == 200, f"HTTP {pc}", f"HTTP {pc}" if pc else "連不到"),
        "任何人都能開" if pc == 200 else "")

    add("")
    for key, label in (("chassis_pids", "模擬底盤"), ("fleetsim_pids", "模擬車隊"),
                       ("agent_pids", "itri-agent")):
        n = len(s[key])
        dup = f"  {RED}⚠ 重複 {n} 份{R}" if n > 1 else ""
        row(label, badge(bool(n)), f"PID {s[key] or '-'}{dup}")

    m = s.get("metrics")
    if m:
        add("")
        add(f"{D}  {'─' * 68}{R}")
        fl, h, mq = m.get("fleet", {}), m.get("history", {}), m.get("mqtt", {})
        b = mq.get("broker_stats") or {}
        add(f"   車隊     {B}{fl.get('online', 0)}/{fl.get('total', 0)}{R} 在線"
            f"   訊息 {fl.get('msg_count', 0):,}"
            f"   異常 {fl.get('faulted', 0)}"
            f"   平均電量 {fl.get('avg_battery', '–')}%")
        add(f"   資料庫   {'連線中' if h.get('db_ready') else RED + '離線' + R}"
            f"   遙測 {h.get('rows_written', 0):,} 列"
            f"   topic {h.get('topics_written', 0):,} 列"
            f"   緩衝 {h.get('buffered', 0) + h.get('topics_buffered', 0)}"
            f"   {'掉 ' + str(h.get('rows_dropped')) if h.get('rows_dropped') else '零丟失'}")
        add(f"   MQTT     連線 {b.get('connects', 0)}"
            f"   認證失敗 {b.get('auth_failures', 0)}"
            f"   ACL 拒絕 {b.get('acl_denied', 0)}")
        add(f"   外網流量 {m.get('rate_bps', 0) / 1024:.1f} KB/s"
            f"   累計 {m.get('total_bytes', 0) / 1e6:.1f} MB"
            f"   推估 {m.get('projected_gb_month', 0)} GB/月"
            f"   分頁 {m.get('ws_clients', 0)}")
        a = s.get("alerts") or {}
        openn = a.get("open") or []
        line = f"   告警     觸發 {a.get('fired', 0)}  恢復 {a.get('resolved', 0)}"
        if openn:
            line += f"   {RED}目前 {len(openn)} 筆觸發中{R}"
        add(line)
        for al in openn[:3]:
            add(f"            {RED}▸ {al['message']}{R}")
    elif s["local_ok"]:
        add(f"\n   {YEL}(無法讀取統計 —— 密碼可能不是 '{PASSWORD}',設 FLEET_PASSWORD){R}")

    return "\n".join(L)


# ------------------------------------------------------------------ actions

def spawn(name: str, args: List[str], cwd: Path = ROOT) -> None:
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"{name}.log", "ab", buffering=0)
    subprocess.Popen([sys.executable, *args], cwd=str(cwd), stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def kill(pids: List[int]) -> int:
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return len(pids)


def wait_for(fn, timeout=25.0, step=0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def start_pg() -> str:
    if pg_running():
        return "PostgreSQL 已經在跑"
    if not (PGBIN / "pg_ctl.exe").exists():
        return f"找不到 {PGBIN}\\pg_ctl.exe"
    subprocess.Popen([str(PGBIN / "pg_ctl.exe"), "-D", str(PGDATA),
                      "-l", str(PGDATA / "server.log"), "start"],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "PostgreSQL 已啟動" if wait_for(pg_running) else "PostgreSQL 啟動逾時"


def stop_pg() -> str:
    if not pg_running():
        return "PostgreSQL 本來就沒在跑"
    subprocess.run([str(PGBIN / "pg_ctl.exe"), "-D", str(PGDATA), "-m", "fast", "stop"],
                   capture_output=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return ("PostgreSQL 已停止(伺服器會把資料暫存在記憶體)"
            if wait_for(lambda: not pg_running(), 30) else "停止逾時")


def start_server() -> str:
    existing = find(SERVICES["server"])
    if existing:
        return f"伺服器已經在跑 (PID {existing})"
    if not pg_running():
        return ("⚠ 請先啟動 PostgreSQL —— mqtt.require_auth=true 時,"
                "資料庫沒起來 broker 會拒絕啟動(fail-closed)")
    spawn("server", ["-m", "server.main"])
    ok = wait_for(lambda: http(f"{BASE}/healthz", 2)[0] == 200, 30)
    return "伺服器已啟動" if ok else "啟動逾時,看 logs/server.log"


def stop_server() -> str:
    n = kill(find(SERVICES["server"]))
    return f"已停止 {n} 個伺服器行程" if n else "伺服器本來就沒在跑"


def start_funnel() -> str:
    subprocess.run([str(TAILSCALE), "funnel", "--bg", "8080"],
                   capture_output=True, text=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return f"Funnel: {funnel_state()}   {PUBLIC}"


def stop_funnel() -> str:
    subprocess.run([str(TAILSCALE), "funnel", "--https=443", "off"],
                   capture_output=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return "Funnel 已關閉 —— 外網立刻斷,tailnet 內部仍可連"


def start_sim() -> str:
    msgs = []
    if not find(SERVICES["chassis"]):
        spawn("chassis", ["tools/sim_chassis.py", "--host", "127.0.0.1", "--port", "1883"])
        msgs.append("模擬底盤已啟動")
    creds = ROOT / "sim_creds.json"
    if not find(SERVICES["agent"]):
        if creds.exists():
            spawn("agent", ["-m", "itri_agent", "run"])
            msgs.append("agent 已啟動")
        else:
            msgs.append("找不到憑證,先跑 itri-agent enroll")
    return " / ".join(msgs) or "模擬環境已經在跑"


def stop_sim() -> str:
    n = kill(find(SERVICES["chassis"]) + find(SERVICES["fleet"]) + find(SERVICES["agent"]))
    return f"已停止 {n} 個模擬行程" if n else "本來就沒有模擬行程"


def kill_duplicates() -> str:
    killed = []
    for label, marker in SERVICES.items():
        pids = find(marker)
        if len(pids) > 1:
            # keep the oldest; the extras are almost always accidental restarts
            kill(pids[1:])
            killed.append(f"{label} x{len(pids) - 1}")
    return "已清除重複:" + ", ".join(killed) if killed else "沒有重複行程"


def start_all() -> str:
    out = [start_pg(), start_server(), start_funnel()]
    return "\n   ".join(out)


def stop_all() -> str:
    return "\n   ".join([stop_funnel(), stop_sim(), stop_server(), stop_pg()])


PGADMIN = Path(os.environ.get("ITRI_PGADMIN")
               or Path.home() / "pgadmin4" / "runtime" / "pgAdmin4.exe")

DSN = os.environ.get("FLEET_DB_DSN") or CFG.get("database", {}).get("dsn", "")

# The handful of questions actually worth asking without opening a GUI.
QUERIES = {
    "1": ("每台車的資料量", """
        SELECT robot_id,
               count(*)                          AS 樣本數,
               count(DISTINCT topic)             AS topic數,
               to_char(min(ts),'MM-DD HH24:MI')  AS 最早,
               to_char(max(ts),'MM-DD HH24:MI')  AS 最新
        FROM topic_samples GROUP BY robot_id ORDER BY 2 DESC"""),
    "2": ("最近 15 筆告警", """
        SELECT to_char(started_at,'MM-DD HH24:MI:SS') AS 開始,
               coalesce(to_char(resolved_at,'HH24:MI:SS'),'進行中') AS 結束,
               robot_id, severity, left(message,60) AS 訊息
        FROM alerts ORDER BY started_at DESC LIMIT 15"""),
    "3": ("磁碟用量(依分區)", """
        SELECT c.relname AS 分區,
               c.reltuples::bigint AS 約略列數,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS 大小
        FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid
        JOIN pg_class p ON p.oid=i.inhparent
        WHERE p.relname IN ('telemetry','topic_samples')
        ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 20"""),
    "4": ("最吵的 topic(前 15)", """
        SELECT topic, sum(samples) AS 累計樣本,
               to_char(max(last_seen),'MM-DD HH24:MI:SS') AS 最後更新
        FROM topic_catalog GROUP BY topic ORDER BY 2 DESC LIMIT 15"""),
    "5": ("車輛註冊狀態", """
        SELECT id, name,
               CASE WHEN revoked_at IS NOT NULL THEN '已撤銷'
                    WHEN secret_hash IS NOT NULL THEN '已發憑證'
                    ELSE '待登記' END AS 憑證,
               to_char(last_seen,'MM-DD HH24:MI:SS') AS 最後回報
        FROM robots ORDER BY id"""),
    "6": ("整體用量摘要", """
        SELECT (SELECT count(*) FROM robots)              AS 註冊車輛,
               (SELECT count(*) FROM telemetry)           AS 遙測列,
               (SELECT count(*) FROM topic_samples)       AS topic列,
               (SELECT count(*) FROM events)              AS 事件,
               (SELECT count(*) FROM alerts)              AS 告警,
               pg_size_pretty(pg_database_size(current_database())) AS 資料庫大小"""),
}


def run_sql(sql: str, limit: int = 200) -> str:
    """Small read-only query runner, so simple lookups do not need an 800 MB GUI."""
    try:
        import psycopg
    except ImportError:
        return "需要 psycopg:pip install \"psycopg[binary]\""
    try:
        with psycopg.connect(DSN, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return f"OK,影響 {cur.rowcount} 列"
                cols = [d.name for d in cur.description]
                rows = cur.fetchmany(limit)
    except Exception as exc:
        return f"{RED}{type(exc).__name__}: {exc}{R}"

    if not rows:
        return "(沒有資料)"
    body = [[("" if v is None else str(v)) for v in r] for r in rows]
    w = [max(width(c), *(width(r[i]) for r in body)) for i, c in enumerate(cols)]
    out = ["  " + "  ".join(f"{B}{pad(c, w[i])}{R}" for i, c in enumerate(cols)),
           "  " + "  ".join("─" * w[i] for i in range(len(cols)))]
    for r in body:
        out.append("  " + "  ".join(pad(v, w[i]) for i, v in enumerate(r)))
    out.append(f"{D}  {len(rows)} 列{' (已截斷)' if len(rows) == limit else ''}{R}")
    return "\n".join(out)


def open_pgadmin() -> str:
    if not PGADMIN.exists():
        return f"找不到 pgAdmin:{PGADMIN}"
    subprocess.Popen([str(PGADMIN)],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return ("pgAdmin 啟動中(第一次要 30 秒左右,會自己開瀏覽器)\n"
            "   連線資訊見 config.yaml 的 database.dsn")


def show_log(name: str, lines: int = 40) -> str:
    candidates = {
        "server": LOGS / "server.log",
        "agent": LOGS / "agent.log",
        "chassis": LOGS / "chassis.log",
        "pg": PGDATA / "server.log",
    }
    p = candidates.get(name)
    if not p or not p.exists():
        return f"找不到 log:{p}"
    data = p.read_bytes()[-40000:].decode("utf-8", "replace").splitlines()
    return "\n".join(data[-lines:])


# --------------------------------------------------------------------- menu

MENU = f"""
{D}  {'─' * 68}{R}
   {B}1{R} 啟動全部        {B}2{R} 停止全部        {B}r{R} 重新整理
   {B}3{R} 資料庫 開/關    {B}4{R} 伺服器 開/關    {B}5{R} 外網 開/關
   {B}6{R} 模擬環境 開/關  {B}7{R} 清除重複行程    {B}8{R} 開啟儀表板
   {B}9{R} 檢視 log        {B}t{R} 執行測試        {B}w{R} 持續監看
   {B}d{R} 開啟 pgAdmin    {B}s{R} 快速 SQL 查詢
   {B}q{R} 離開(不會關掉任何服務)
"""

SQL_MENU = f"""
{D}  {'─' * 68}{R}
   完整功能請用 {B}d{R} 開啟 pgAdmin —— 這裡只是常用查詢的捷徑。
""" + "\n".join(f"   {B}{k}{R} {name}" for k, (name, _) in QUERIES.items()) + f"""
   {B}x{R} 自己輸入 SQL       {B}Enter{R} 返回
"""


def clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


# One-shot actions, so the .bat launchers can stay pure ASCII and all the
# Chinese output happens here. cmd.exe parses a batch file using the system
# codepage before `chcp 65001` can take effect, so non-ASCII bytes in a UTF-8
# .bat get mis-decoded and can split a line into a bogus command.
ACTIONS = {
    "start-all": start_all, "stop-all": stop_all,
    "pg-start": start_pg, "pg-stop": stop_pg,
    "server-start": start_server, "server-stop": stop_server,
    "funnel-on": start_funnel, "funnel-off": stop_funnel,
    "sim-start": start_sim, "sim-stop": stop_sim,
    "dedupe": kill_duplicates, "pgadmin": open_pgadmin,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="印一次就結束")
    ap.add_argument("--watch", action="store_true", help="持續刷新")
    ap.add_argument("--action", choices=sorted(ACTIONS), help="執行單一動作就結束")
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    os.system("")          # enables ANSI colours in older cmd.exe

    if args.action:
        print(f"{B}{CYA}  {args.action}{R}")
        print("  " + ACTIONS[args.action]())
        print()
        print(render(collect()))
        return 0

    if args.status:
        print(render(collect()))
        return 0

    if args.watch:
        try:
            while True:
                st = collect()
                clear()
                print(render(st))
                print(f"\n{D}   每 {args.interval}s 更新,Ctrl-C 結束{R}")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    note = ""
    while True:
        st = collect()
        clear()
        print(render(st))
        if note:
            print(f"\n   {CYA}{note}{R}")
            note = ""
        print(MENU)
        try:
            c = input("   > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0

        if c == "q":
            return 0
        elif c == "r":
            continue
        elif c == "1":
            note = start_all()
        elif c == "2":
            if input("   確定要停止全部?外網會立刻斷線 (y/N) ").strip().lower() == "y":
                note = stop_all()
        elif c == "3":
            note = stop_pg() if st["pg"] else start_pg()
        elif c == "4":
            note = stop_server() if st["server_pids"] else start_server()
        elif c == "5":
            note = stop_funnel() if st["funnel"] == "on" else start_funnel()
        elif c == "6":
            note = stop_sim() if (st["chassis_pids"] or st["agent_pids"]) else start_sim()
        elif c == "7":
            note = kill_duplicates()
        elif c == "8":
            url = PUBLIC if st["public_code"] == 200 else BASE
            os.startfile(url) if hasattr(os, "startfile") else None
            note = f"已開啟 {url}"
        elif c == "9":
            which = input("   哪一個? server / agent / chassis / pg > ").strip() or "server"
            clear()
            print(show_log(which))
            input("\n   按 Enter 返回 ")
        elif c == "t":
            clear()
            for script in ("tools/test_enroll.py", "tools/test_broker_acl.py"):
                if (ROOT / script).exists():
                    print(f"\n{B}--- {script} ---{R}")
                    subprocess.run([sys.executable, script], cwd=str(ROOT))
            input("\n   按 Enter 返回 ")
        elif c == "d":
            note = open_pgadmin()
        elif c == "s":
            while True:
                clear()
                print(f"{B}{CYA}  快速 SQL 查詢{R}   {D}{DSN.split('@')[-1]}{R}")
                print(SQL_MENU)
                pick = input("   > ").strip().lower()
                if not pick:
                    break
                if pick == "x":
                    sql = input("   SQL > ").strip()
                    if not sql:
                        continue
                elif pick in QUERIES:
                    sql = QUERIES[pick][1]
                else:
                    continue
                clear()
                print(run_sql(sql))
                input("\n   按 Enter 返回 ")
        elif c == "w":
            try:
                while True:
                    clear()
                    print(render(collect()))
                    print(f"\n{D}   監看中,Ctrl-C 返回選單{R}")
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                note = "已離開監看模式"
    return 0


if __name__ == "__main__":
    sys.exit(main())
