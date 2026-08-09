"""Offline tests for the ingest protocol and the presence state machine.

No server, no database, no broker -- these are the pure decision rules, which
are exactly the parts where a wrong answer is silent. Run with:

    python tools/test_protocol.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.ingest import TelemetryRouter                      # noqa: E402
from server.registry import Registry                           # noqa: E402
from server.state import FleetState, OK, STALE, OFFLINE, UNKNOWN  # noqa: E402

for _s in (sys.stdout, sys.stderr):      # cp950 console cannot print CJK
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ok_n = fail_n = 0


def check(label, cond, extra=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print(f"  [OK]   {label} {extra}")
    else:
        fail_n += 1
        print(f"  [FAIL] {label} {extra}")


def section(t):
    print(f"\n=== {t} ===")


# --------------------------------------------------------------- presence

section("1. 四態:UNKNOWN / OK / STALE / OFFLINE")

fs = FleetState(offline_after=5.0, stale_after=30.0)
fs.robots.setdefault("r1", None)
del fs.robots["r1"]

fs.ingest("r1", {"battery": 80})
r = fs.robots["r1"]
check("收到資料 -> OK", r.presence == OK, f"-> {r.presence}")

r.last_seen = time.time() - 10          # 超過 offline_after,未超過 stale_after
fs.reap()
check("沉默 10s(>5,<30)-> STALE", r.presence == STALE, f"-> {r.presence}")
check("STALE 不算 online", r.online is False)

r.last_seen = time.time() - 100
fs.reap()
check("沉默 100s(>30)-> OFFLINE", r.presence == OFFLINE, f"-> {r.presence}")

section("2. 註冊但從未回報 = UNKNOWN,不是 OK")

fs.set_link("r2", True)                  # LWT 說連上了,但沒有任何資料
check("只有 LWT online 仍是 UNKNOWN",
      fs.robots["r2"].presence == UNKNOWN, f"-> {fs.robots['r2'].presence}")
check("UNKNOWN 不計入 online", fs.summary()["online"] == 0,
      f"-> online={fs.summary()['online']}")

section("3. LWT offline 是最強訊號,勝過新鮮的資料")

fs.ingest("r3", {"battery": 50})
check("先是 OK", fs.robots["r3"].presence == OK)
fs.set_link("r3", False)
check("LWT offline -> 立刻 OFFLINE",
      fs.robots["r3"].presence == OFFLINE, f"-> {fs.robots['r3'].presence}")

section("4. ★ 連線還在但資料停了 —— 布林模型看不見的那一種")

fs.ingest("r4", {"battery": 90})
fs.set_link("r4", True)                  # broker 認為連線正常,不會發 LWT
fs.robots["r4"].last_seen = time.time() - 12
fs.reap()
check("link=True 但資料停 12s -> STALE",
      fs.robots["r4"].presence == STALE, f"-> {fs.robots['r4'].presence}")
check("link 仍記錄為 True", fs.robots["r4"].link is True)
s = fs.summary()
check("summary 分開統計 stale", s["stale"] == 1, f"-> {s['stale']}")
check("summary 分開統計 unknown", s["unknown"] == 1, f"-> {s['unknown']}")

section("5. 恢復")

fs.ingest("r4", {"battery": 89})
check("再收到資料 -> OK", fs.robots["r4"].presence == OK)
check("reap 回報變化", "r4" not in fs.reap())

# ----------------------------------------------------------------- dedup

section("6. QoS 1 重送去重(robot_id + boot_id + seq)")

got = []
router = TelemetryRouter(FleetState(), on_raw=lambda rid, b: got.append((rid, b)))


def send(rid, boot, seq, rows):
    payload = json.dumps({"v": 1, "id": rid, "boot": boot, "seq": seq,
                          "ts": time.time(), "b": rows}).encode()
    router.handle(f"fleet/{rid}/samples", payload)


send("r1", "aaa", 1, [["t/a", 1.0, 5, 0]])
send("r1", "aaa", 1, [["t/a", 1.0, 5, 0]])          # 完全相同的重送
check("重送的同一批被丟棄", len(got) == 1, f"-> 收下 {len(got)} 批")
check("重複計數 = 1", router.duplicates == 1, f"-> {router.duplicates}")

send("r1", "aaa", 2, [["t/a", 2.0, 6, 0]])
check("下一個 seq 正常收下", len(got) == 2)

send("r1", "bbb", 1, [["t/a", 3.0, 7, 0]])
check("agent 重開(新 boot_id)後 seq=1 不會被誤判為重複", len(got) == 3)

send("r2", "aaa", 1, [["t/a", 1.0, 5, 0]])
check("不同車輛的相同 seq 不互相干擾", len(got) == 4)

section("7. 舊版 agent(沒有信封)仍然收得下")

router.handle("fleet/r9/raw", json.dumps({"b": [["t/x", 1.0, 1]]}).encode())
check("舊格式被接受", len(got) == 5)
check("計為 unversioned", router.unversioned == 1, f"-> {router.unversioned}")
check("舊的 raw 主題仍然路由", got[-1][0] == "r9")

section("8. 新的 samples 主題")

router.handle("fleet/r8/samples", json.dumps(
    {"v": 1, "id": "r8", "boot": "z", "seq": 1, "b": [["t/y", 1.0, 2, 1]]}).encode())
check("samples 主題被路由", got[-1][0] == "r8")

section("9. 去重視窗有上限(長跑不會漏記憶體)")

small = TelemetryRouter(FleetState(), dedup_window=64)
for i in range(500):
    small.handle("fleet/rz/samples", json.dumps(
        {"boot": "b", "seq": i, "b": [["t", 1.0, i, 0]]}).encode())
check("視窗被裁切到上限", len(small._recent) <= 64, f"-> {len(small._recent)}")
check("沒有誤判成重複", small.duplicates == 0, f"-> {small.duplicates}")

# ------------------------------------------------------------------- ACL

section("10. ACL:只能發佈,不能訂閱")

t = Registry.topic_allowed
check("可發佈自己的 samples", t("carA", "fleet/carA/samples", "publish"))
check("可發佈自己的 raw(舊名相容)", t("carA", "fleet/carA/raw", "publish"))
check("可發佈自己的 status", t("carA", "fleet/carA/status", "publish"))
check("不能發佈他車", not t("carA", "fleet/carB/status", "publish"))
check("★ 訂閱自己的 cmd 也被拒(下行已移除)",
      not t("carA", "fleet/carA/cmd", "subscribe"))
check("訂閱自己的 samples 被拒", not t("carA", "fleet/carA/samples", "subscribe"))
check("萬用字元被拒", not t("carA", "fleet/+/status", "subscribe"))
check("全域萬用字元被拒", not t("carA", "#", "subscribe"))
check("空使用者被拒", not t("", "fleet/carA/status", "publish"))

print(f"\n{'=' * 46}\n通過 {ok_n} / {ok_n + fail_n}\n{'=' * 46}")
sys.exit(1 if fail_n else 0)
