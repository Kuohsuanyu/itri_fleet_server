"""itri-agent command line.

    itri-agent enroll --server https://<你的節點>.<你的tailnet>.ts.net --token XXXX-XXXX-XXXX
    itri-agent discover                 # live topic table, pick by number
    itri-agent run                      # relay the selection
    itri-agent status
    itri-agent install-service          # systemd, Linux only

Enrollment uses urllib from the standard library rather than requests/httpx --
one fewer wheel to install on a Raspberry Pi.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from . import config as cfgmod
from .bridge import Bridge
from .discover import estimate_cost, parse_selection, scan

BOLD, DIM, GREEN, RED, YELLOW, RESET = (
    "\x1b[1m", "\x1b[2m", "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[0m")


def _utf8_stdout() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------- enroll

def cmd_enroll(args) -> int:
    body = json.dumps({"token": args.token,
                       "hostname": socket.gethostname()}).encode()
    url = args.server.rstrip("/") + "/api/enroll"
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            cred = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        print(f"{RED}登記失敗 HTTP {exc.code}{RESET}: {detail}")
        if exc.code == 403:
            print("token 可能已被使用或過期,請到管理頁重新產生。")
        return 1
    except urllib.error.URLError as exc:
        print(f"{RED}連不到伺服器{RESET} {url}: {exc.reason}")
        print("這台機器需要能連到伺服器 —— 如果走 Tailscale,先確認 `tailscale status`。")
        return 1

    cred["server"] = args.server.rstrip("/")
    path = cfgmod.save_credentials(cred)
    cfg = cfgmod.load_config()
    if args.local_host:
        cfg["local"]["host"] = args.local_host
    if args.local_port:
        cfg["local"]["port"] = args.local_port
    cfgmod.save_config(cfg)

    print(f"{GREEN}登記成功{RESET}")
    print(f"  robot id : {cred['robot_id']}")
    print(f"  broker   : {cred['mqtt']['host']}:{cred['mqtt']['port']}")
    print(f"  憑證     : {path}  (權限 0600)")
    print(f"\n下一步:{BOLD}itri-agent discover{RESET}  掃描本地 topic 並選擇要轉發哪些")
    return 0


# ---------------------------------------------------------------------- setup

def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        return input(f"{prompt}{hint}: ").strip() or default
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)


def cmd_setup(args) -> int:
    """Interactive first-run wizard.

    Running the bare command lands here, because the natural expectation after
    installing is to be asked what to do -- not to have to remember the exact
    `enroll --server ... --token ...` invocation.
    """
    print(f"\n{BOLD}itri-agent 設定精靈{RESET}")
    print(f"{DIM}{'─' * 58}{RESET}")

    existing = cfgmod.load_credentials()
    if existing:
        print(f"這台機器已經登記為 {BOLD}{existing['robot_id']}{RESET}"
              f"(伺服器 {existing.get('server', '?')})")
        if _ask("要重新登記嗎?會作廢現有憑證 (y/N)", "N").lower() != "y":
            print(f"\n保留現有設定。接著可以跑 {BOLD}itri-agent discover{RESET} 或 "
                  f"{BOLD}itri-agent run{RESET}")
            return 0

    print("\n步驟 1/3 —— 授權碼")
    print(f"{DIM}  到伺服器的管理頁 /admin/robots 按「+ 新增車輛」,{RESET}")
    print(f"{DIM}  會拿到一組像 XXXX-XXXX-XXXX 的一次性授權碼(預設 15 分鐘內有效)。{RESET}\n")

    server = args.server or _ask("  伺服器網址",
                                 (existing or {}).get("server", "https://"))
    token = args.token or _ask("  授權碼")
    if not token:
        print(f"{RED}沒有授權碼就無法登記。{RESET}")
        return 1

    ns = argparse.Namespace(server=server, token=token,
                            local_host=None, local_port=None)
    if cmd_enroll(ns) != 0:
        return 1

    print("\n步驟 2/3 —— 本地 broker")
    cfg = cfgmod.load_config()
    host = _ask("  上位機自己的 MQTT broker 位址", cfg["local"]["host"])
    port = _ask("  port", str(cfg["local"]["port"]))
    cfg["local"]["host"] = host
    try:
        cfg["local"]["port"] = int(port)
    except ValueError:
        pass
    cfgmod.save_config(cfg)

    print("\n步驟 3/3 —— 選擇要轉發的 topic")
    if _ask("  現在掃描本地 topic 嗎? (Y/n)", "Y").lower() != "n":
        rc = cmd_discover(argparse.Namespace(host=None, port=None,
                                             seconds=args.scan_seconds, yes=False))
        if rc != 0:
            print(f"{YELLOW}掃描沒有成功,之後可以再跑 itri-agent discover。{RESET}")

    print(f"\n{GREEN}設定完成{RESET}")
    print(f"  啟動轉發   {BOLD}itri-agent run{RESET}")
    print(f"  開機自動跑 {BOLD}itri-agent install-service{RESET}")
    print(f"\n{DIM}授權碼只用這一次。之後斷線、重開機都會用存在本機的憑證自動重連,{RESET}")
    print(f"{DIM}不需要再拿新的授權碼 —— 除非管理員把這台車撤銷。{RESET}")
    return 0


# ------------------------------------------------------------------- discover

def cmd_discover(args) -> int:
    cfg = cfgmod.load_config()
    host = args.host or cfg["local"]["host"]
    port = args.port or cfg["local"]["port"]

    try:
        stats = scan(host, int(port), seconds=args.seconds,
                     username=cfg["local"].get("username"),
                     password=cfg["local"].get("password"),
                     subscribe=["#"], exclude=cfg.get("exclude") or [])
    except (ConnectionError, OSError) as exc:
        print(f"{RED}無法連上本地 broker {host}:{port}{RESET}: {exc}")
        print("上位機上真的有 MQTT broker 嗎?用 --host/--port 指定其他位址。")
        return 1

    if not stats:
        print(f"{YELLOW}沒有收到任何 topic。{RESET} 底盤程式有在發嗎?")
        return 1

    topics = sorted(stats)
    print(f"{BOLD}掃到 {len(topics)} 個 topic{RESET}")
    if args.yes:
        chosen = topics
    else:
        print("輸入要轉發的編號,例如 1,3,5-8;全部輸入 all;直接 Enter 取消")
        try:
            picked = parse_selection(input("> "), len(topics))
        except (ValueError, EOFError) as exc:
            print(f"{RED}{exc}{RESET}")
            return 1
        if not picked:
            print("取消,設定未變更。")
            return 0
        chosen = [topics[i - 1] for i in picked]

    cfg = cfgmod.load_config()
    cost = estimate_cost(stats, chosen, float(cfg["max_rate_hz"]),
                         bool(cfg["on_change_only"]))
    print(f"\n{BOLD}已選 {len(chosen)} 個 topic{RESET}")
    for t in chosen:
        print(f"  {t}")
    print(f"\n{DIM}預估上傳 {cost['rows_per_s']:.1f} 筆/秒 → "
          f"{cost['gb_per_day']:.2f} GB/天 → {cost['gb_per_month']:.1f} GB/月(單台車)"
          f"{RESET}")
    print(f"{DIM}(已計入 max_rate_hz={cfg['max_rate_hz']} 與 "
          f"on_change_only={cfg['on_change_only']}){RESET}")

    cfg["include"] = chosen
    path = cfgmod.save_config(cfg)
    print(f"\n寫入 {path}")
    print(f"下一步:{BOLD}itri-agent run{RESET}")
    return 0


# ------------------------------------------------------------------------ run

def cmd_run(args) -> int:
    cred = cfgmod.load_credentials()
    if not cred:
        print(f"{RED}尚未登記{RESET} —— 先跑 `itri-agent enroll --server ... --token ...`")
        return 1
    cfg = cfgmod.load_config()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    inc = cfg.get("include") or []
    print(f"{BOLD}itri-agent{RESET}  {cred['robot_id']}  →  "
          f"{cred['mqtt']['host']}:{cred['mqtt']['port']}")
    print(f"  本地 broker : {cfg['local']['host']}:{cfg['local']['port']}")
    print(f"  轉發        : {len(inc) if inc else '全部'} 個 topic")
    print(f"  節流        : max {cfg['max_rate_hz']} Hz/topic, "
          f"on_change_only={cfg['on_change_only']}\n")

    bridge = Bridge(cfg, cred)
    last = [0.0]

    def tick(s: Dict[str, Any]) -> None:
        if time.time() - last[0] < 5:
            return
        last[0] = time.time()
        state = f"{GREEN}上行正常{RESET}" if s["uplink"] else f"{RED}上行斷線{RESET}"
        print(f"  {state}  topic {s['topics']:>3}  收 {s['seen']:>7}  "
              f"轉 {s['relayed']:>7}  批次 {s['batches']:>5}  "
              f"緩衝 {s['buffered']:>6}  "
              f"略過(頻率 {s['skipped_rate']} / 重複 {s['skipped_same']} / "
              f"過大 {s['skipped_big']})  掉 {s['dropped']}")

    bridge.run(on_tick=tick)
    print("\n已停止")
    return 0


# --------------------------------------------------------------------- status

def cmd_status(args) -> int:
    cred = cfgmod.load_credentials()
    cfg = cfgmod.load_config()
    print(f"{BOLD}設定{RESET} {cfgmod.CONFIG_PATH}")
    print(f"  本地 broker : {cfg['local']['host']}:{cfg['local']['port']}")
    inc = cfg.get("include") or []
    print(f"  轉發        : {len(inc) if inc else '全部'} 個 topic")
    print(f"  排除        : {', '.join(cfg.get('exclude') or []) or '無'}")
    print(f"  節流        : max_rate_hz={cfg['max_rate_hz']} "
          f"on_change_only={cfg['on_change_only']} deadband={cfg['deadband']}")
    print(f"  對應欄位    : {cfg.get('map') or '無(儀表板只會顯示上線狀態)'}")
    print(f"\n{BOLD}憑證{RESET} {cfgmod.CRED_PATH}")
    if not cred:
        print(f"  {YELLOW}尚未登記{RESET}")
        return 1
    print(f"  robot id : {cred['robot_id']}")
    print(f"  伺服器   : {cred.get('server', '?')}")
    print(f"  broker   : {cred['mqtt']['host']}:{cred['mqtt']['port']}")
    return 0


# ------------------------------------------------------------------- service

SYSTEMD_UNIT = """\
[Unit]
Description=ITRI Fleet Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
ExecStart={python} -m itri_agent run
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
{home_env}

[Install]
WantedBy=multi-user.target
"""


def cmd_install_service(args) -> int:
    if not sys.platform.startswith("linux"):
        print(f"{RED}只支援 Linux(systemd){RESET};這台是 {sys.platform}")
        return 1
    import getpass
    import os
    home_env = ""
    if os.environ.get("ITRI_AGENT_HOME"):
        home_env = f"Environment=ITRI_AGENT_HOME={os.environ['ITRI_AGENT_HOME']}"
    unit = SYSTEMD_UNIT.format(user=getpass.getuser(), python=sys.executable,
                               home_env=home_env)
    path = "/etc/systemd/system/itri-agent.service"
    print(unit)
    print(f"{DIM}把上面存成 {path},然後:{RESET}")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now itri-agent")
    print("  journalctl -u itri-agent -f")
    return 0


# ------------------------------------------------------------------------ main

def main(argv: Optional[List[str]] = None) -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(prog="itri-agent",
                                 description="ITRI 車隊 agent:把上位機的 MQTT topic 轉發到伺服器")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("setup", help="互動式設定精靈(不帶參數執行也會進來)")
    p.add_argument("--server")
    p.add_argument("--token")
    p.add_argument("--scan-seconds", type=float, default=12.0)
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("enroll", help="用一次性 token 換取本車憑證")
    p.add_argument("--server", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--local-host", help="上位機本地 broker 位址(預設 127.0.0.1)")
    p.add_argument("--local-port", type=int)
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("discover", help="即時列出本地所有 topic 並選擇要轉發哪些")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--seconds", type=float, default=0.0, help="0 = 直到 Ctrl-C")
    p.add_argument("-y", "--yes", action="store_true", help="不問,全選")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("run", help="開始轉發")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="顯示目前設定與憑證")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("install-service", help="產生 systemd unit")
    p.set_defaults(func=cmd_install_service)

    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        # bare `itri-agent` -> the wizard, which is what someone who just
        # installed it is looking for
        return cmd_setup(argparse.Namespace(server=None, token=None,
                                            scan_seconds=12.0))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
