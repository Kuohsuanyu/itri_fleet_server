"""Bulk-create and enrol simulated robots through the real API.

This is not a shortcut around enrollment -- it drives exactly the same
endpoints a real robot's agent will call, so running it exercises the whole
credential path end to end.

  python tools/provision_sim.py -n 12 --password itri --out sim_creds.json
  python tools/sim_robots.py --credentials sim_creds.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=12)
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--password", default="itri", help="dashboard password")
    ap.add_argument("--prefix", default="amr")
    ap.add_argument("--out", default="sim_creds.json")
    ap.add_argument("--reuse", action="store_true",
                    help="re-issue tokens for robots that already exist")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    base = args.server.rstrip("/")
    admin = {"Authorization": f"Bearer {args.password}"}
    creds = {}
    made = reused = failed = 0

    with httpx.Client(timeout=30.0) as c:
        r = c.get(f"{base}/healthz")
        r.raise_for_status()

        for i in range(1, args.count + 1):
            rid = f"{args.prefix}-{i:02d}"
            name = f"{args.prefix.upper()}-{i:02d}"

            resp = c.post(f"{base}/api/admin/robots", headers=admin,
                          json={"name": name, "id": rid})
            if resp.status_code == 400 and "already exists" in resp.text:
                if not args.reuse:
                    print(f"  {rid}: 已存在,略過(要重發憑證加 --reuse)")
                    failed += 1
                    continue
                resp = c.post(f"{base}/api/admin/robots/{rid}/token", headers=admin)
                reused += 1
            else:
                made += 1
            if resp.status_code != 200:
                print(f"  {rid}: 建立失敗 {resp.status_code} {resp.text[:120]}")
                failed += 1
                continue

            token = resp.json()["token"]
            enr = c.post(f"{base}/api/enroll",
                         json={"token": token, "hostname": f"sim-{rid}"})
            if enr.status_code != 200:
                print(f"  {rid}: 登記失敗 {enr.status_code} {enr.text[:120]}")
                failed += 1
                continue

            d = enr.json()
            creds[rid] = {"username": d["mqtt_username"],
                          "password": d["mqtt_password"],
                          "host": d["mqtt"]["host"], "port": d["mqtt"]["port"]}
            print(f"  {rid}: OK  {d['mqtt_password'][:10]}...")

    out = Path(args.out)
    out.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    print(f"\n新建 {made} / 重發 {reused} / 失敗 {failed}")
    print(f"憑證寫入 {out.resolve()}")
    print("⚠ 這個檔案含明文密鑰,不要提交到版本控制(.gitignore 已排除)")
    return 1 if failed and not creds else 0


if __name__ == "__main__":
    sys.exit(main())
