"""End-to-end check of the registry + enrollment flow."""
import sys, json, httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
B = "http://127.0.0.1:8080"
ADMIN = {"Authorization": "Bearer itri"}
c = httpx.Client(timeout=20.0)
ok = fail = 0

# Unique id per run so this never collides with a provisioned fleet, and so a
# failed run does not poison the next one.
import secrets
RID = f"test-{secrets.token_hex(3)}"
RNAME = RID.upper()


def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  [OK]   {label} {extra}")
    else:
        fail += 1; print(f"  [FAIL] {label} {extra}")


print("=== 1. 新增車輛(含中文標籤)===")
r = c.post(f"{B}/api/admin/robots", headers=ADMIN,
           json={"name": RNAME, "id": RID, "tags": ["A區", "堆高機"]})
check("HTTP 200", r.status_code == 200, f"-> {r.status_code} {r.text[:120]}")
created = r.json()
print(f"    id      : {created['id']}")
print(f"    token   : {created['token']}")
print(f"    install : {created['install']}")
token = created["token"]

print("\n=== 2. 列表顯示為「待登記」===")
lst = c.get(f"{B}/api/admin/robots", headers=ADMIN).json()
row = next(x for x in lst["robots"] if x["id"] == RID)
check("未登記前 enrolled=False", row["enrolled"] is False)
check("有 pending token", bool(row["pending_token"]))
check("中文標籤正確存取", row["tags"] == ["A區", "堆高機"], f"-> {row['tags']}")

print("\n=== 3. 車上 agent 登記 ===")
r = c.post(f"{B}/api/enroll", json={"token": token, "hostname": "amr07-nuc"})
check("HTTP 200", r.status_code == 200, f"-> {r.status_code} {r.text[:120]}")
cred = r.json()
secret = cred["mqtt_password"]
print(f"    robot_id : {cred['robot_id']}")
print(f"    username : {cred['mqtt_username']}")
print(f"    password : {secret[:14]}...  ({len(secret)} 字元)")
print(f"    broker   : {cred['mqtt']['host']}:{cred['mqtt']['port']}")
print(f"    topic    : {cred['mqtt']['status_topic']}")

print("\n=== 4. 安全性檢查 ===")
r = c.post(f"{B}/api/enroll", json={"token": token})
check("token 不能重複使用", r.status_code == 403, f"-> {r.status_code} {r.json().get('detail')}")
r = c.post(f"{B}/api/enroll", json={"token": "ZZZZ-ZZZZ-ZZZZ"})
check("亂猜 token 被拒", r.status_code == 403, f"-> {r.status_code}")
r = c.get(f"{B}/api/admin/robots")
check("管理 API 需要登入", r.status_code == 401, f"-> {r.status_code}")
r = c.post(f"{B}/api/admin/robots", headers=ADMIN, json={"name": RNAME, "id": RID})
check("重複 id 被拒", r.status_code == 400, f"-> {r.status_code} {r.json().get('detail')}")

print("\n=== 5. 登記後狀態 ===")
lst = c.get(f"{B}/api/admin/robots", headers=ADMIN).json()
row = next(x for x in lst["robots"] if x["id"] == RID)
check("enrolled=True", row["enrolled"] is True)
check("pending token 已清除", row["pending_token"] is None)

print("\n=== 6. 撤銷 ===")
r = c.post(f"{B}/api/admin/robots/{RID}/revoke", headers=ADMIN)
check("撤銷成功", r.status_code == 200)
row = next(x for x in c.get(f"{B}/api/admin/robots", headers=ADMIN).json()["robots"]
           if x["id"] == RID)
check("revoked_at 已設定", row["revoked_at"] is not None)
check("憑證已清除", row["enrolled"] is False)

print("\n=== 7. 稽核軌跡 ===")
# events are buffered and flushed once per second, so give the writer a tick
import time
time.sleep(2.5)
evs = c.get(f"{B}/api/events?robot_id={RID}&kind=enroll", headers=ADMIN).json()
enroll_kinds = [e["detail"].get("action") for e in evs["events"] if e["detail"]]
print(f"    enroll 事件: {enroll_kinds}")
check("有 created 事件", "created" in enroll_kinds)
check("有 enrolled 事件", "enrolled" in enroll_kinds)
rv = c.get(f"{B}/api/events?robot_id={RID}&kind=revoke", headers=ADMIN).json()
print(f"    revoke 事件: {[e['detail'] for e in rv['events']]}")
check("有 revoke 事件", rv["count"] >= 1)
ip_logged = any(e["detail"] and e["detail"].get("ip") for e in
                c.get(f"{B}/api/events?kind=enroll", headers=ADMIN).json()["events"])
check("登記來源 IP 有記錄", ip_logged)

print("\n=== 8. 管理頁面可載入 ===")
# Every admin page and the scripts they load. Listing them individually is the
# point: the admin used to be one page, and when it was split the old test kept
# passing against a file that no longer existed.
r = c.get(f"{B}/admin", headers=ADMIN)
check("/admin 轉向 /admin/robots",
      r.status_code in (301, 302, 307, 308)
      and r.headers.get("location", "").endswith("/admin/robots"),
      f"-> {r.status_code} {r.headers.get('location', '')}")

for path in ("/admin/robots", "/admin/topics", "/admin/alerts",
             "/admin/events", "/admin/system",
             "/static/admin_common.js", "/static/admin_robots.js",
             "/static/admin_topics.js", "/static/admin_alerts.js",
             "/static/admin_events.js", "/static/admin_system.js",
             "/static/shell.js", "/static/push.js",
             "/static/admin.css", "/static/style.css"):
    r = c.get(f"{B}{path}", headers=ADMIN)
    check(path, r.status_code == 200, f"-> {r.status_code}, {len(r.content)} bytes")

print(f"\n{'='*46}\n通過 {ok} / {ok+fail}\n{'='*46}")
sys.exit(1 if fail else 0)
