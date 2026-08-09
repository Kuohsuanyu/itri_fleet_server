"""第一次安裝精靈:從 git clone 之後到系統跑起來,一步一步帶完。

    python tools/setup.py

做的事,每一步都可以跳過或重跑:
  1. 檢查 Python 與相依套件
  2. 找到或安裝 PostgreSQL(可下載免安裝版,不需要系統管理員權限)
  3. 問你資料要存哪裡,initdb 並啟動
  4. 建立資料庫與帳號
  5. 產生 Web Push 的 VAPID 金鑰
  6. 設定儀表板密碼
  7. 檢查 Tailscale、指引開通 Funnel
  8. 建立 schema、驗證整條路徑

產出的 config.yaml 在 .gitignore 裡,不會進版本控制。
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
EXAMPLE = ROOT / "config.example.yaml"

B, D, R = "\x1b[1m", "\x1b[2m", "\x1b[0m"
GRN, RED, YEL, CYA = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[36m"

PG_VERSION = "17.6-1"
PG_URL = f"https://get.enterprisedb.com/postgresql/postgresql-{PG_VERSION}-windows-x64-binaries.zip"


def say(msg: str = "") -> None:
    print(msg)


def step(n: int, total: int, title: str) -> None:
    print(f"\n{B}{CYA}[{n}/{total}] {title}{R}")
    print(f"{D}{'─' * 64}{R}")


def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        return input(f"  {prompt}{hint}: ").strip() or default
    except (EOFError, KeyboardInterrupt):
        print("\n  已取消")
        raise SystemExit(1)


def yes(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    a = ask(f"{prompt} ({d})").lower()
    return default if not a else a.startswith("y")


def ok(msg: str) -> None:
    print(f"  {GRN}OK{R}  {msg}")


def warn(msg: str) -> None:
    print(f"  {YEL}!{R}   {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}X{R}   {msg}")


# --------------------------------------------------------------------- steps

def step_python() -> bool:
    step(1, 8, "檢查 Python 與相依套件")
    v = sys.version_info
    if v < (3, 9):
        bad(f"Python {v.major}.{v.minor} 太舊,需要 3.9 以上")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")

    missing = []
    for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
                     ("paho.mqtt", "paho-mqtt"), ("yaml", "PyYAML"),
                     ("psycopg", "psycopg[binary,pool]"), ("httpx", "httpx"),
                     ("pywebpush", "pywebpush"), ("websockets", "websockets")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        ok("相依套件齊全")
        return True

    warn(f"缺少:{', '.join(missing)}")
    if yes("現在安裝嗎?"):
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                            str(ROOT / "requirements.txt")])
        if r.returncode == 0:
            ok("安裝完成")
            return True
        bad("安裝失敗,請手動 pip install -r requirements.txt")
        return False
    return False


def find_pg() -> Optional[Path]:
    """已安裝的 PostgreSQL bin 目錄。"""
    for cand in (os.environ.get("ITRI_PGBIN"),
                 r"C:\Users\%s\pgsql\bin" % os.environ.get("USERNAME", ""),
                 *(rf"C:\Program Files\PostgreSQL\{v}\bin" for v in (18, 17, 16, 15))):
        if cand and (Path(cand) / "pg_ctl.exe").exists():
            return Path(cand)
    w = shutil.which("pg_ctl")
    return Path(w).parent if w else None


def download_pg(dest: Path) -> Optional[Path]:
    """免安裝版 —— zip 解壓即可,不需要系統管理員權限。"""
    say(f"  下載 PostgreSQL {PG_VERSION}(約 330 MB)…")
    tmp = dest.parent / "_pg.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(PG_URL, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("content-length", 0))
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    print(f"\r    {pct:3d}%  {got/1e6:.0f}/{total/1e6:.0f} MB", end="")
        print()
    except Exception as exc:
        bad(f"下載失敗:{exc}")
        tmp.unlink(missing_ok=True)
        return None

    say("  解壓縮…")
    stage = dest.parent / "_pgstage"
    shutil.rmtree(stage, ignore_errors=True)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(stage)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(stage / "pgsql"), str(dest))
    shutil.rmtree(stage, ignore_errors=True)
    tmp.unlink(missing_ok=True)
    ok(f"PostgreSQL 解壓到 {dest}")
    return dest / "bin"


def step_postgres() -> Optional[Tuple[Path, Path]]:
    step(2, 8, "PostgreSQL")
    pgbin = find_pg()
    if pgbin:
        ok(f"找到既有安裝:{pgbin}")
    else:
        warn("找不到 PostgreSQL")
        say(f"{D}    免安裝版:解壓即用,不需要系統管理員權限,也不會影響系統。{R}")
        if not yes("要下載免安裝版嗎?"):
            bad("沒有資料庫就無法繼續 —— 裝好後重跑這支")
            return None
        home = Path(ask("  安裝到哪個資料夾",
                        str(Path.home() / "pgsql")))
        pgbin = download_pg(home)
        if not pgbin:
            return None

    say()
    say(f"{D}  資料目錄放的是實際的資料庫檔案。50 台車保留一個月約需 40 GB,{R}")
    say(f"{D}  建議放在空間充足的磁碟。{R}")
    default_data = str(Path.home() / "pgdata-itri")
    pgdata = Path(ask("  資料目錄", default_data))

    if (pgdata / "PG_VERSION").exists():
        ok(f"資料目錄已存在:{pgdata}")
    else:
        pw = secrets.token_urlsafe(18)
        say(f"  initdb 到 {pgdata} …")
        pwfile = pgdata.parent / "_pw.txt"
        pgdata.parent.mkdir(parents=True, exist_ok=True)
        pwfile.write_text(pw, encoding="ascii")
        r = subprocess.run([str(pgbin / "initdb.exe"), "-D", str(pgdata),
                            "-U", "itri", "--auth=scram-sha-256",
                            f"--pwfile={pwfile}", "-E", "UTF8", "--locale=C"],
                           capture_output=True, text=True)
        pwfile.unlink(missing_ok=True)
        if r.returncode != 0:
            bad(f"initdb 失敗:{r.stderr[-300:]}")
            return None
        ok("資料目錄已初始化")

        conf = pgdata / "postgresql.conf"
        conf.write_text(conf.read_text(encoding="utf-8") + f"""
# ---- ITRI fleet 調校 ----
listen_addresses = 'localhost'
shared_buffers = 2GB
effective_cache_size = 8GB
work_mem = 32MB
maintenance_work_mem = 512MB
# 遙測是 append-only 的資料流,硬斷電時損失最後幾百毫秒可以接受,
# 但每筆 insert 都等 fsync 不行。
synchronous_commit = off
wal_compression = on
max_wal_size = 4GB
checkpoint_timeout = 15min
checkpoint_completion_target = 0.9
""", encoding="utf-8")
        (pgdata / "_itri_pw").write_text(pw, encoding="ascii")
        ok("已寫入效能調校設定")

    if not port_open("127.0.0.1", 5432):
        say("  啟動 PostgreSQL …")
        subprocess.Popen([str(pgbin / "pg_ctl.exe"), "-D", str(pgdata),
                          "-l", str(pgdata / "server.log"), "start"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(40):
            if port_open("127.0.0.1", 5432):
                break
            time.sleep(0.5)
    if port_open("127.0.0.1", 5432):
        ok("PostgreSQL 執行中(127.0.0.1:5432)")
        return pgbin, pgdata
    bad(f"啟動失敗,看 {pgdata / 'server.log'}")
    return None


def port_open(host: str, port: int, t: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(t)
        return s.connect_ex((host, port)) == 0


def step_database(pgbin: Path, pgdata: Path) -> Optional[str]:
    step(3, 8, "建立資料庫")
    stored = pgdata / "_itri_pw"
    pw = stored.read_text(encoding="ascii").strip() if stored.exists() else ""
    if not pw:
        pw = getpass.getpass("  PostgreSQL 使用者 itri 的密碼: ").strip()
    env = dict(os.environ, PGPASSWORD=pw)
    dbname = ask("  資料庫名稱", "itri_fleet")

    r = subprocess.run([str(pgbin / "psql.exe"), "-h", "127.0.0.1", "-U", "itri",
                        "-d", "postgres", "-t", "-A", "-c",
                        f"SELECT 1 FROM pg_database WHERE datname='{dbname}'"],
                       env=env, capture_output=True, text=True)
    if "1" in r.stdout:
        ok(f"資料庫 {dbname} 已存在")
    else:
        r = subprocess.run([str(pgbin / "createdb.exe"), "-h", "127.0.0.1",
                            "-U", "itri", dbname], env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad(f"建立失敗:{r.stderr[-200:]}")
            return None
        ok(f"資料庫 {dbname} 已建立")
    return f"postgresql://itri:{pw}@127.0.0.1:5432/{dbname}"


def gen_vapid() -> Tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    pub = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return b64(pub), b64(priv)


def step_config(dsn: str) -> Optional[dict]:
    step(4, 8, "產生設定檔")
    if CONFIG.exists():
        warn(f"{CONFIG.name} 已存在")
        if not yes("要覆蓋嗎?(會先備份)", False):
            ok("保留現有設定")
            return {}
        shutil.copy2(CONFIG, CONFIG.with_suffix(f".yaml.bak-{int(time.time())}"))

    text = EXAMPLE.read_text(encoding="utf-8")

    say(f"{D}  儀表板會透過 Tailscale Funnel 公開在網際網路上,{R}")
    say(f"{D}  這組密碼是唯一擋在前面的東西。{R}")
    pw = ask("  儀表板密碼(留空自動產生 24 字元)")
    if not pw:
        pw = secrets.token_urlsafe(18)
        ok(f"已產生:{B}{pw}{R}   ← 記下來")
    elif len(pw) < 12:
        warn("少於 12 字元,公開在網路上很容易被試出來")

    pub, priv = gen_vapid()
    ok("已產生 Web Push VAPID 金鑰")
    contact = ask("  聯絡信箱(推播服務要求,不會外流)", "you@example.com")

    text = re.sub(r"(^\s*password:)\s*CHANGE_ME", rf"\1 {pw}", text, flags=re.M)
    text = re.sub(r"dsn:\s*\S+", f"dsn: {dsn}", text)
    text = text.replace("public_key: GENERATED_BY_SETUP", f"public_key: {pub}")
    text = text.replace("private_key: GENERATED_BY_SETUP", f"private_key: {priv}")
    text = re.sub(r"(contact:)\s*\S+", rf"\1 mailto:{contact}", text)
    text = re.sub(r"^# -{20,}\n(#[^\n]*\n)+\n", "", text, count=1)

    CONFIG.write_text(text, encoding="utf-8")
    ok(f"已寫入 {CONFIG}")
    say(f"{D}  這個檔在 .gitignore 裡,不會被 commit。{R}")
    return {"password": pw}


def step_tailscale() -> None:
    step(5, 8, "Tailscale")
    ts = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not Path(ts).exists():
        warn("找不到 Tailscale")
        say("    下載:https://tailscale.com/download")
        say("    裝好後重跑這一步,或手動執行 tailscale up")
        return
    r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=20)
    try:
        data = json.loads(r.stdout)
        dns = (data["Self"]["DNSName"] or "").rstrip(".")
        ok(f"已登入,本機節點:{dns}")
        say(f"\n{D}  公開網址就是節點名稱。要換名字:{R}")
        say(f"    tailscale set --hostname=<你要的名字>")
        say(f"\n{D}  還沒開通的話,到 admin console 點三個開關:{R}")
        say("    1. HTTPS Certificates  https://login.tailscale.com/admin/dns")
        say("    2/3. 執行 tailscale funnel --bg 8080,它會印出授權連結")
    except Exception:
        warn("Tailscale 尚未登入 —— 執行 tailscale up")


def step_schema() -> bool:
    step(6, 8, "建立資料表")
    r = subprocess.run([sys.executable, "-c",
                        "import asyncio,sys;sys.path.insert(0,'.');"
                        "from server.main import CFG;"
                        "from server.db import Database;"
                        "async def m():\n"
                        "    d=Database(CFG['database']['dsn']);await d.start();"
                        "    print('READY' if d.ready else 'FAIL:'+str(d.last_error));"
                        "    await d.stop()\n"
                        "asyncio.run(m())"], cwd=str(ROOT),
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if "READY" in out:
        ok("schema 已建立(資料表、每日分區、索引)")
        return True
    bad(f"失敗:{out[-400:]}")
    return False


def step_verify(info: dict) -> None:
    step(7, 8, "驗證")
    say("  啟動伺服器測試 …")
    p = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    up = False
    for _ in range(40):
        if port_open("127.0.0.1", 8080):
            up = True
            break
        if p.poll() is not None:
            break
        time.sleep(0.5)
    if up:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=5) as r:
                ok(f"伺服器回應 {r.status} {r.read().decode().strip()}")
        except Exception as exc:
            warn(f"啟動了但無回應:{exc}")
    else:
        bad("伺服器沒有起來")
        out = p.stdout.read() if p.stdout else ""
        print(out[-800:])
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()


def step_done(info: dict) -> None:
    step(8, 8, "完成")
    pw = info.get("password")
    say(f"""
  接下來:

    {B}scripts\\0_控制台.bat{R}          一個畫面看完狀態,並可開關全部服務
    {B}scripts\\3_開啟外網.bat{R}        掛上 Tailscale Funnel

  儀表板   http://localhost:8080{'   密碼 ' + B + pw + R if pw else ''}
  管理頁   http://localhost:8080/admin/robots

  新增第一台車:管理頁按「+ 新增車輛」,把它給的指令貼到上位機上。

{D}  資料庫圖形介面請裝 pgAdmin 4,或用 scripts\\9_資料庫介面.bat。{R}
""")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    os.system("")

    print(f"\n{B}{CYA}  ITRI Fleet Server 安裝精靈{R}")
    print(f"{D}  每一步都可以中斷,重跑會接續而不是從頭來。{R}")

    if not step_python():
        return 1
    pg = step_postgres()
    if not pg:
        return 1
    dsn = step_database(*pg)
    if not dsn:
        return 1
    info = step_config(dsn)
    if info is None:
        return 1
    step_tailscale()
    if not step_schema():
        return 1
    step_verify(info)
    step_done(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
