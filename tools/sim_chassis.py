"""Simulate a chassis vendor's onboard computer.

Publishes to a LOCAL broker using topic names nobody on the server side has ever
seen -- which is the whole point: itri-agent must relay them without knowing
what they mean.

  python tools/sim_chassis.py --host 127.0.0.1 --port 1883

Mosquitto already listens on 127.0.0.1:1883 on this machine, so it stands in for
the vehicle's own broker.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import signal
import sys
import time

import paho.mqtt.client as mqtt

# Deliberately messy, vendor-flavoured naming: mixed depth, mixed payload types.
TOPICS = [
    ("chassis/bat_pct",          "num",   0.5),
    ("chassis/bat_voltage",      "num",   0.5),
    ("chassis/mode",             "enum",  0.2),
    ("chassis/vel/linear",       "num",   5.0),
    ("chassis/vel/angular",      "num",   5.0),
    ("chassis/motor/1/temp_c",   "num",   1.0),
    ("chassis/motor/2/temp_c",   "num",   1.0),
    ("chassis/motor/1/current_a", "num",  5.0),
    ("chassis/motor/2/current_a", "num",  5.0),
    ("chassis/estop",            "bool",  0.2),
    ("chassis/odom_m",           "num",   1.0),
    ("sensors/imu/yaw_deg",      "num",  10.0),
    ("sensors/ultrasonic/front", "num",   5.0),
    ("sensors/wifi_rssi",        "num",   0.5),
    ("diag/fault_code",          "text",  0.2),
    ("diag/uptime_s",            "num",   0.5),
    ("nav/task_id",              "text",  0.1),
    ("nav/progress",             "num",   1.0),
    ("nav/waypoint",             "json",  0.5),
]

MODES = ["idle", "moving", "charging", "error"]
FAULTS = ["", "", "", "E014_wheel_slip", "E203_lidar_timeout"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="", help="topic prefix, e.g. robot1/")
    ap.add_argument("--rate-scale", type=float, default=1.0)
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-chassis")
    c.connect(args.host, args.port, keepalive=30)
    c.loop_start()

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)

    next_at = {t: 0.0 for t, _, _ in TOPICS}
    battery, odom, t0 = 92.0, 4120.0, time.time()
    mode = "moving"
    sent = 0
    print(f"模擬底盤上位機 -> {args.host}:{args.port}   {len(TOPICS)} 個 topic   Ctrl-C 停止")

    while running:
        now = time.time()
        el = now - t0
        if random.random() < 0.002:
            mode = random.choice(MODES)
        if mode == "charging":
            battery = min(100.0, battery + 0.05)
        elif mode == "moving":
            battery = max(0.0, battery - 0.004)
            odom += 0.4

        for topic, kind, hz in TOPICS:
            period = 1.0 / (hz * args.rate_scale)
            if now < next_at[topic]:
                continue
            next_at[topic] = now + period

            if topic == "chassis/bat_pct":
                v = round(battery, 1)
            elif topic == "chassis/bat_voltage":
                v = round(44.0 + battery * 0.06, 2)
            elif topic == "chassis/mode":
                v = mode
            elif topic == "chassis/odom_m":
                v = round(odom, 1)
            elif topic == "diag/uptime_s":
                v = int(el)
            elif topic == "chassis/estop":
                v = mode == "error"
            elif topic == "diag/fault_code":
                v = random.choice(FAULTS) if mode == "error" else ""
            elif topic == "nav/task_id":
                v = f"T-{int(el / 120) + 1000}"
            elif topic == "nav/progress":
                v = round((el % 120) / 120, 3)
            elif topic == "nav/waypoint":
                v = json.dumps({"x": round(3 * math.sin(el / 7), 2),
                                "y": round(3 * math.cos(el / 9), 2)})
            elif kind == "num":
                base = {"chassis/vel/linear": 0.6, "chassis/vel/angular": 0.1,
                        "chassis/motor/1/temp_c": 42, "chassis/motor/2/temp_c": 44,
                        "chassis/motor/1/current_a": 3.2, "chassis/motor/2/current_a": 3.4,
                        "sensors/imu/yaw_deg": 0, "sensors/ultrasonic/front": 2.0,
                        "sensors/wifi_rssi": -55}.get(topic, 1.0)
                amp = abs(base) * 0.25 + 0.5
                v = round(base + amp * math.sin(el * 0.7 + hash(topic) % 7), 3)
                if mode != "moving" and topic.startswith("chassis/vel"):
                    v = 0.0
            else:
                v = "?"

            c.publish(args.prefix + topic, str(v) if not isinstance(v, str) else v, qos=0)
            sent += 1

        time.sleep(0.02)

    c.loop_stop()
    c.disconnect()
    print(f"\n停止,共發出 {sent} 筆")
    return 0


if __name__ == "__main__":
    sys.exit(main())
