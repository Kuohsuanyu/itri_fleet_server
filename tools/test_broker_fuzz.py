"""Protocol robustness for the hand-written MQTT broker.

The ACL tests prove the broker enforces the right policy on well-formed
traffic. This proves it survives traffic that is not well-formed -- which is
the other half, and the half a hand-written protocol parser is most likely to
get wrong.

Everything here is a client the broker should reject or ignore *without*
dying: after each case the broker must still serve a normal client. That final
check is the real assertion. A broker that raises inside its read loop and
takes the task down with it fails silently, because the web server keeps
running and the dashboard just stops updating.

Runs its own broker on a spare port. No database, no config, no network beyond
loopback:

    python tools/test_broker_fuzz.py
"""

import asyncio
import os
import random
import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from server.broker import MqttBroker      # noqa: E402

PORT = 18830
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


# ------------------------------------------------------------------ raw client

def connect_packet(client_id="probe", username=None, password=None):
    """A minimal, valid MQTT 3.1.1 CONNECT."""
    payload = struct.pack("!H", len(client_id)) + client_id.encode()
    flags = 0x02                                   # clean session
    if username:
        flags |= 0x80
        payload += struct.pack("!H", len(username)) + username.encode()
    if password:
        flags |= 0x40
        payload += struct.pack("!H", len(password)) + password.encode()
    var = struct.pack("!H", 4) + b"MQTT" + bytes([4, flags]) + struct.pack("!H", 30)
    body = var + payload
    return bytes([0x10]) + encode_len(len(body)) + body


def encode_len(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            return bytes(out)


def send_raw(data, read=True, timeout=2.0):
    """Send bytes, return whatever comes back (b'' if the peer just closed)."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
    try:
        s.sendall(data)
        if not read:
            return b""
        s.settimeout(timeout)
        try:
            return s.recv(256)
        except (socket.timeout, ConnectionResetError, OSError):
            return b""
    finally:
        try:
            s.close()
        except OSError:
            pass


def broker_alive():
    """A well-formed CONNECT must still get a CONNACK."""
    r = send_raw(connect_packet("healthy"))
    return len(r) >= 4 and r[0] == 0x20


async def main():
    broker = MqttBroker(host="127.0.0.1", port=PORT,
                        authenticate=lambda u, p: u == "good" and p == "pw",
                        authorize=lambda u, t, a: a == "publish")
    await broker.start()
    await asyncio.sleep(0.2)
    loop = asyncio.get_running_loop()

    async def probe(fn, *a):
        return await loop.run_in_executor(None, fn, *a)

    try:
        section("0. 基準:正常客戶端可以連上")
        check("乾淨的 CONNECT 拿到 CONNACK", await probe(broker_alive))

        section("1. 畸形封包")

        cases = [
            ("空的連線(連一個位元組都沒送)", b"", False),
            ("只送一個位元組", b"\x10", True),
            ("宣告長度 200 但只給 3 bytes", b"\x10" + encode_len(200) + b"abc", True),
            ("宣告長度 0 的 CONNECT", b"\x10\x00", True),
            ("非法的封包型別 0", b"\x00\x00", True),
            ("非法的封包型別 15", b"\xf0\x00", True),
            ("protocol name 不是 MQTT", bytes([0x10]) + encode_len(12) +
             struct.pack("!H", 4) + b"XXXX" + bytes([4, 2]) + struct.pack("!H", 30) +
             struct.pack("!H", 0), True),
            ("protocol level 是 99", bytes([0x10]) + encode_len(12) +
             struct.pack("!H", 4) + b"MQTT" + bytes([99, 2]) + struct.pack("!H", 30) +
             struct.pack("!H", 0), True),
            ("字串長度欄位超出封包", bytes([0x10]) + encode_len(8) +
             struct.pack("!H", 9999) + b"MQTT" + bytes([4, 2]), True),
            ("剩餘長度用了 5 個連續位元組(違反規格上限 4)",
             b"\x10\xff\xff\xff\xff\xff", True),
            ("先送 PUBLISH,沒有 CONNECT", b"\x30\x05\x00\x03abc", True),
            ("先送 SUBSCRIBE,沒有 CONNECT", b"\x82\x05\x00\x01\x00\x00\x00", True),
            ("CONNECT 之後緊接著垃圾",
             connect_packet("x") + os.urandom(64), True),
        ]
        for label, data, _ in cases:
            await probe(send_raw, data, True, 1.5)
            alive = await probe(broker_alive)
            check(label, alive, "-> broker 仍然存活" if alive else "-> BROKER 死了")

        section("2. 超長欄位")

        long_id = "c" * 8000
        await probe(send_raw, connect_packet(long_id), True, 2.0)
        check("8000 字元的 client id", await probe(broker_alive))

        # topic longer than the 500-char column, published after a real CONNECT
        long_topic = "a/" * 5000
        pkt = connect_packet("good", "good", "pw")
        tb = long_topic.encode()
        pub = bytes([0x30]) + encode_len(2 + len(tb) + 3) + \
            struct.pack("!H", len(tb)) + tb + b"xyz"
        await probe(send_raw, pkt + pub, True, 2.0)
        check("10000 字元的 topic", await probe(broker_alive))

        big = b"z" * 300_000
        pub = bytes([0x30]) + encode_len(2 + 3 + len(big)) + \
            struct.pack("!H", 3) + b"a/b" + big
        await probe(send_raw, pkt + pub, True, 3.0)
        check("300 KB 的 payload", await probe(broker_alive))

        section("3. 切成兩半的封包 / 慢速傳送")

        def dribble():
            data = connect_packet("slow")
            s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
            try:
                for b in data:                     # 一次一個位元組
                    s.sendall(bytes([b]))
                s.settimeout(2.0)
                try:
                    return s.recv(64)
                except (socket.timeout, OSError):
                    return b""
            finally:
                s.close()

        r = await probe(dribble)
        check("逐位元組送出的 CONNECT 仍被正確組回",
              len(r) >= 4 and r[0] == 0x20, f"-> {r[:4]!r}")

        def half_then_close():
            data = connect_packet("halfy")
            s = socket.create_connection(("127.0.0.1", PORT), timeout=2)
            s.sendall(data[:len(data) // 2])
            s.close()                              # 送一半就斷
        await probe(half_then_close)
        check("送一半就斷線", await probe(broker_alive))

        section("4. 連線耗盡")

        def flood(n):
            socks = []
            for _ in range(n):
                try:
                    s = socket.create_connection(("127.0.0.1", PORT), timeout=2)
                    s.sendall(connect_packet(f"f{len(socks)}"))
                    socks.append(s)
                except OSError:
                    break
            return socks

        socks = await probe(flood, 200)
        check(f"同時開 {len(socks)} 條連線", len(socks) >= 100, f"-> {len(socks)} 條")
        check("洪水期間仍能服務新客戶端", await probe(broker_alive))
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        await asyncio.sleep(0.4)
        check("全部斷開後仍然正常", await probe(broker_alive))

        def open_and_abandon(n):
            """開著不送任何資料 —— slowloris 的最簡形式。"""
            socks = []
            for _ in range(n):
                try:
                    socks.append(socket.create_connection(("127.0.0.1", PORT),
                                                          timeout=2))
                except OSError:
                    break
            return socks

        idle = await probe(open_and_abandon, 100)
        check("100 條打開但完全不說話的連線", await probe(broker_alive))
        for s in idle:
            try:
                s.close()
            except OSError:
                pass

        section("5. 隨機位元組轟炸")

        random.seed(20260809)
        for i in range(60):
            n = random.randint(1, 400)
            await probe(send_raw, bytes(random.getrandbits(8) for _ in range(n)),
                        False, 1.0)
        await asyncio.sleep(0.3)
        check("60 組隨機位元組之後 broker 仍存活", await probe(broker_alive))

        section("6. 認證與 ACL 在畸形輸入之後依然生效")

        r = await probe(send_raw, connect_packet("bad", "bad", "nope"))
        check("錯誤憑證仍被拒絕", len(r) >= 4 and r[3] != 0, f"-> CONNACK {r[3:4]!r}")
        r = await probe(send_raw, connect_packet("good", "good", "pw"))
        check("正確憑證仍被接受", len(r) >= 4 and r[3] == 0, f"-> CONNACK {r[3:4]!r}")

    finally:
        await broker.stop()

    print(f"\n{'=' * 46}\n通過 {ok_n} / {ok_n + fail_n}\n{'=' * 46}")
    return 1 if fail_n else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
