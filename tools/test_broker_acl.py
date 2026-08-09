"""Verify that the broker actually enforces authentication and topic ACL.

The whole value of the archive rests on this: if any client on the network can
publish as any robot, the history stops being evidence. These checks assert the
failure paths, not the happy path.

  python tools/test_broker_acl.py --credentials sim_creds.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx
import paho.mqtt.client as mqtt

ok = fail = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1; print(f"  [OK]   {label} {extra}")
    else:
        fail += 1; print(f"  [FAIL] {label} {extra}")


# paho 2.x normalises MQTT 3.1.1 CONNACK codes onto the MQTT 5 reason-code
# space, so "not authorized" arrives as 135 (0x87) rather than 5. Accept both.
DENIED = (5, 135)


def rejected(rc) -> bool:
    return rc in DENIED


def connect(host, port, user, pw, timeout=6.0):
    """Returns (client, connack_rc). rc 0 = accepted, 5/135 = not authorized."""
    result = {}
    done = []

    def on_connect(c, u, flags, rc, props=None):
        result["rc"] = int(getattr(rc, "value", rc))
        done.append(True)

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=(user or "anon") + "-probe")
    c.on_connect = on_connect
    if user is not None:
        c.username_pw_set(user, pw)
    try:
        c.connect(host, port, keepalive=20)
    except Exception as exc:
        return None, f"connect-error: {exc}"
    c.loop_start()
    t0 = time.time()
    while not done and time.time() - t0 < timeout:
        time.sleep(0.05)
    if not done:
        c.loop_stop()
        return None, "timeout"
    return c, result.get("rc")


def sub_result(client, topic, timeout=4.0):
    """Returns the granted QoS, or 128 for an ACL refusal."""
    got = {}
    client.on_subscribe = lambda c, u, mid, rcs, props=None: got.update(
        rc=int(getattr(rcs[0], "value", rcs[0])))
    client.subscribe(topic, qos=0)
    t0 = time.time()
    while "rc" not in got and time.time() - t0 < timeout:
        time.sleep(0.05)
    return got.get("rc", "timeout")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credentials", default="sim_creds.json")
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--password", default="itri")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    creds = json.load(open(args.credentials, encoding="utf-8"))
    ids = sorted(creds)
    a, b = ids[0], ids[1]
    ca, cb = creds[a], creds[b]
    host, port = ca["host"], ca["port"]
    admin = {"Authorization": f"Bearer {args.password}"}
    http = httpx.Client(timeout=20.0)

    print(f"broker {host}:{port}   受測身分 {a} / {b}\n")

    print("=== 1. 認證 ===")
    c, rc = connect(host, port, None, None)
    check("匿名連線被拒", rejected(rc), f"-> CONNACK {rc}")
    if c: c.loop_stop()

    c, rc = connect(host, port, a, "wrong-password")
    check("錯誤密碼被拒", rejected(rc), f"-> CONNACK {rc}")
    if c: c.loop_stop()

    c, rc = connect(host, port, "does-not-exist", "whatever")
    check("不存在的帳號被拒", rejected(rc), f"-> CONNACK {rc}")
    if c: c.loop_stop()

    ca_client, rc = connect(host, port, a, ca["password"])
    check("正確憑證可連線", rc == 0, f"-> CONNACK {rc}")
    if rc != 0:
        print("無法繼續"); return 1

    print("\n=== 2. Topic ACL(關鍵)===")
    base = http.get(f"{args.server}/api/metrics", headers=admin).json()
    denied0 = base["mqtt"]["broker_stats"]["acl_denied"]

    ca_client.publish(f"fleet/{a}/status", json.dumps({"battery": 50}), qos=1)
    ca_client.publish(f"fleet/{b}/status", json.dumps({"battery": 1}), qos=1)   # 冒充
    ca_client.publish("fleet/evil/status", json.dumps({"battery": 1}), qos=1)
    ca_client.publish("$SYS/hack", b"x", qos=1)
    time.sleep(1.5)

    after = http.get(f"{args.server}/api/metrics", headers=admin).json()
    denied = after["mqtt"]["broker_stats"]["acl_denied"] - denied0
    check("冒充他車 / 亂發主題被擋", denied >= 3, f"-> 擋下 {denied} 筆(預期 3)")

    fleet = http.get(f"{args.server}/api/fleet", headers=admin).json()
    victim = next((r for r in fleet["robots"] if r["id"] == b), None)
    check("受害車輛電量未被竄改",
          victim is None or victim["battery"] is None or victim["battery"] > 5,
          f"-> {b} battery={victim['battery'] if victim else 'n/a'}")
    check("不存在的 evil 車輛沒有被建立",
          not any(r["id"] == "evil" for r in fleet["robots"]))

    check("可訂閱自己的 cmd", sub_result(ca_client, f"fleet/{a}/cmd") == 0)
    check("訂閱他車 cmd 被拒", sub_result(ca_client, f"fleet/{b}/cmd") == 128)
    check("萬用字元訂閱被拒", sub_result(ca_client, "fleet/+/status") == 128)
    check("全域萬用字元被拒", sub_result(ca_client, "#") == 128)

    print("\n=== 3. 撤銷立即生效 ===")
    r = http.post(f"{args.server}/api/admin/robots/{a}/revoke", headers=admin)
    killed = r.json().get("sessions_killed", 0)
    check("撤銷時踢掉現有連線", killed >= 1, f"-> 踢掉 {killed} 條")
    time.sleep(1.0)
    c2, rc = connect(host, port, a, ca["password"])
    check("撤銷後無法重新連線", rejected(rc), f"-> CONNACK {rc}")
    if c2: c2.loop_stop()

    print("\n=== 4. 復原 ===")
    old_password = ca["password"]      # ca aliases creds[a]; copy before mutating
    tok = http.post(f"{args.server}/api/admin/robots/{a}/token", headers=admin).json()
    new = http.post(f"{args.server}/api/enroll", json={"token": tok["token"]}).json()
    creds[a]["password"] = new["mqtt_password"]
    json.dump(creds, open(args.credentials, "w", encoding="utf-8"), indent=2)
    time.sleep(0.5)
    c3, rc = connect(host, port, a, new["mqtt_password"])
    check("重新登記後可連線", rc == 0, f"-> CONNACK {rc}")
    check("舊密鑰已失效", rejected(connect(host, port, a, old_password)[1]))
    if c3: c3.loop_stop()
    ca_client.loop_stop()

    print(f"\n{'='*46}\n通過 {ok} / {ok+fail}\n{'='*46}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
