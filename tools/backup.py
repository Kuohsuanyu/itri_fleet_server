"""備份與搬機:把整台伺服器打包成一個檔,或從那個檔還原。

  python tools/backup.py export                   完整備份(含歷史資料)
  python tools/backup.py export --no-history      只帶設定與註冊表(搬機用,快很多)
  python tools/backup.py restore <bundle.zip>     在新機器上還原
  python tools/backup.py list                     看現有備份

包進去的東西:
  config.yaml         含 VAPID 私鑰與儀表板密碼
  資料庫 pg_dump      車輛註冊、憑證雜湊、預警規則、歷史遙測
  agent wheel         讓新機器不用重新 build 就能發套件

不包的東西(刻意):
  Tailscale 身分      節點金鑰不能複製,新機器要自己 `tailscale up`
  logs/               沒有保存價值
  sim_creds.json      模擬用的明文密鑰
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / "backups"
PGBIN = Path(os.environ.get("ITRI_PGBIN") or Path.home() / "pgsql" / "bin")


def dsn_parts(dsn: str) -> dict:
    u = urlparse(dsn)
    return {"host": u.hostname or "127.0.0.1", "port": str(u.port or 5432),
            "user": u.username or "itri", "password": u.password or "",
            "db": (u.path or "/itri_fleet").lstrip("/")}


def read_dsn() -> str:
    if os.environ.get("FLEET_DB_DSN"):
        return os.environ["FLEET_DB_DSN"]
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("database", {}).get("dsn", "")


def pg_tool(name: str) -> str:
    local = PGBIN / f"{name}.exe"
    return str(local) if local.exists() else (shutil.which(name) or name)


# ---------------------------------------------------------------------- export

def cmd_export(args) -> int:
    dsn = read_dsn()
    p = dsn_parts(dsn)
    BACKUPS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "config" if args.no_history else "full"
    out = BACKUPS / f"itri-fleet-{tag}-{stamp}.zip"
    dump = BACKUPS / f"_dump-{stamp}.sql"

    env = dict(os.environ, PGPASSWORD=p["password"])
    cmd = [pg_tool("pg_dump"), "-h", p["host"], "-p", p["port"], "-U", p["user"],
           "-d", p["db"], "--no-owner", "--no-privileges", "-f", str(dump)]
    if args.no_history:
        # schema for everything, rows only for the small operational tables:
        # a config-only bundle should not drag 100 MB of telemetry along
        cmd += ["--exclude-table-data=telemetry*", "--exclude-table-data=topic_samples*"]

    print(f"  pg_dump {p['db']} ...")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  pg_dump 失敗:{r.stderr.strip()[:300]}")
        dump.unlink(missing_ok=True)
        return 1
    print(f"    {dump.stat().st_size / 1e6:.1f} MB")

    wheels = sorted((ROOT / "agent" / "dist").glob("*.whl"))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(dump, "database.sql")
        z.write(ROOT / "config.yaml", "config.yaml")
        if wheels:
            z.write(wheels[-1], f"agent/{wheels[-1].name}")
        z.writestr("MANIFEST.json", json.dumps({
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": tag,
            "database": p["db"],
            "wheel": wheels[-1].name if wheels else None,
            "note": "Tailscale 身分不在裡面,新機器要自己 tailscale up",
        }, indent=2, ensure_ascii=False))
    dump.unlink()

    print(f"\n  備份完成:{out}")
    print(f"  {out.stat().st_size / 1e6:.1f} MB")
    if args.no_history:
        print("  (不含歷史遙測 —— 搬機用這個就好,快很多)")
    print("\n  ⚠ 這個檔案含 VAPID 私鑰、儀表板密碼、資料庫密碼,當機密保管。")
    return 0


# --------------------------------------------------------------------- restore

def cmd_restore(args) -> int:
    bundle = Path(args.bundle)
    if not bundle.exists():
        print(f"  找不到 {bundle}")
        return 1

    z = zipfile.ZipFile(bundle)
    manifest = json.loads(z.read("MANIFEST.json").decode("utf-8"))
    print(f"  備份建立於 {manifest['created']}  類型 {manifest['kind']}")

    dsn = read_dsn()
    p = dsn_parts(dsn)
    print(f"  將還原到 {p['user']}@{p['host']}:{p['port']}/{p['db']}")
    if not args.yes:
        if input("  這會覆蓋目標資料庫的同名資料表,繼續? (y/N) ").strip().lower() != "y":
            return 1

    tmp = BACKUPS / "_restore.sql"
    BACKUPS.mkdir(exist_ok=True)
    tmp.write_bytes(z.read("database.sql"))

    env = dict(os.environ, PGPASSWORD=p["password"])
    # create the database if the new machine does not have it yet
    subprocess.run([pg_tool("createdb"), "-h", p["host"], "-p", p["port"],
                    "-U", p["user"], p["db"]], env=env, capture_output=True)

    print("  psql 匯入中 ...")
    r = subprocess.run([pg_tool("psql"), "-h", p["host"], "-p", p["port"],
                        "-U", p["user"], "-d", p["db"], "-v", "ON_ERROR_STOP=0",
                        "-f", str(tmp)], env=env, capture_output=True, text=True)
    tmp.unlink(missing_ok=True)
    errs = [l for l in (r.stderr or "").splitlines() if "ERROR" in l]
    if errs:
        print(f"  匯入時有 {len(errs)} 個錯誤(多半是既有物件,可忽略):")
        for e in errs[:5]:
            print(f"    {e[:110]}")

    if not args.keep_config and "config.yaml" in z.namelist():
        target = ROOT / "config.yaml"
        if target.exists():
            backup = target.with_suffix(f".yaml.before-restore-{int(time.time())}")
            shutil.copy2(target, backup)
            print(f"  舊 config 備份到 {backup.name}")
        target.write_bytes(z.read("config.yaml"))
        print("  config.yaml 已還原")

    for name in z.namelist():
        if name.startswith("agent/") and name.endswith(".whl"):
            dest = ROOT / "agent" / "dist" / Path(name).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
            print(f"  agent wheel 已還原:{dest.name}")

    print("\n  還原完成。接下來:")
    print("    1. tailscale up --hostname=itri     讓網址跟舊機器一樣")
    print("    2. tailscale funnel --bg 8080")
    print("    3. scripts\\0_控制台.bat  → 1 啟動全部")
    print("\n  已登記的車輛不用重新登記 —— 憑證雜湊在資料庫裡跟著搬過來了。")
    return 0


def cmd_list(args) -> int:
    if not BACKUPS.exists():
        print("  還沒有備份")
        return 0
    rows = sorted(BACKUPS.glob("itri-fleet-*.zip"))
    if not rows:
        print("  還沒有備份")
        return 0
    for f in rows:
        print(f"  {f.name:<44} {f.stat().st_size / 1e6:>8.1f} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="打包備份")
    e.add_argument("--no-history", action="store_true",
                   help="不含歷史遙測(搬機建議用這個)")
    e.set_defaults(func=cmd_export)
    r = sub.add_parser("restore", help="從備份還原")
    r.add_argument("bundle")
    r.add_argument("-y", "--yes", action="store_true")
    r.add_argument("--keep-config", action="store_true", help="不要覆蓋現有 config.yaml")
    r.set_defaults(func=cmd_restore)
    l = sub.add_parser("list", help="列出備份")
    l.set_defaults(func=cmd_list)

    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
