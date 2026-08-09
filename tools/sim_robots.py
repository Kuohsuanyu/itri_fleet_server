"""Fake wheeled-robot fleet publishing MQTT telemetry.

Use it to exercise the dashboard and to measure Funnel egress before real
hardware exists.

  python tools/sim_robots.py                 # 6 robots @ 2 Hz
  python tools/sim_robots.py -n 40 --hz 5    # stress the pipeline
  python tools/sim_robots.py --host <server-tailscale-ip>   # from another machine
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

STATES = ["idle", "moving", "charging"]


class SimRobot:
    def __init__(self, idx: int, area: float):
        self.id = f"amr-{idx:02d}"
        self.name = f"AMR-{idx:02d}"
        self.x = random.uniform(-area, area)
        self.y = random.uniform(-area, area)
        self.yaw = random.uniform(-math.pi, math.pi)
        self.battery = random.uniform(45, 100)
        self.state = "moving"
        self.v = 0.0
        self.w = 0.0
        self.odom = random.uniform(0, 20000)
        self.mission = f"T-{random.randint(1000, 9999)}"
        self.progress = random.random()
        self.errors: list[str] = []
        self.area = area
        self._t = random.uniform(0, 100)

    def step(self, dt: float) -> None:
        self._t += dt

        # occasional state changes so the dashboard has something to show
        if random.random() < 0.004:
            self.state = random.choice(STATES)
        if self.battery < 18 and self.state != "charging":
            self.state = "charging"
        if self.state == "charging":
            self.battery = min(100.0, self.battery + 4.0 * dt)
            self.v = self.w = 0.0
            if self.battery > 97:
                self.state = "moving"
        elif self.state == "moving":
            self.v = 0.5 + 0.35 * math.sin(self._t * 0.4)
            self.w = 0.45 * math.sin(self._t * 0.23)
            self.battery = max(0.0, self.battery - 0.05 * dt)
        else:
            self.v = self.w = 0.0
            self.battery = max(0.0, self.battery - 0.01 * dt)

        self.yaw = (self.yaw + self.w * dt + math.pi) % (2 * math.pi) - math.pi
        self.x += self.v * math.cos(self.yaw) * dt
        self.y += self.v * math.sin(self.yaw) * dt
        self.odom += abs(self.v) * dt

        # bounce off the arena walls instead of wandering off the map
        if abs(self.x) > self.area or abs(self.y) > self.area:
            self.yaw += math.pi
            self.x = max(-self.area, min(self.area, self.x))
            self.y = max(-self.area, min(self.area, self.y))

        if self.state == "moving":
            self.progress = min(1.0, self.progress + 0.01 * dt)
            if self.progress >= 1.0:
                self.mission = f"T-{random.randint(1000, 9999)}"
                self.progress = 0.0

        if random.random() < 0.0006:
            self.errors = [random.choice(["lidar_timeout", "wheel_slip",
                                          "obstacle_blocked", "imu_drift"])]
            self.state = "error"
        elif self.errors and random.random() < 0.02:
            self.errors = []
            self.state = "moving"

    def payload(self) -> bytes:
        return json.dumps({
            "id": self.id,
            "name": self.name,
            "ts": round(time.time(), 2),
            "state": self.state,
            "battery": round(self.battery, 1),
            "pose": {"x": round(self.x, 3), "y": round(self.y, 3), "yaw": round(self.yaw, 3)},
            "vel": {"v": round(self.v, 3), "w": round(self.w, 3)},
            "mission": {"id": self.mission, "progress": round(self.progress, 3)},
            "errors": self.errors,
            "temp": round(38 + 6 * math.sin(self._t * 0.05), 1),
            "wifi": round(-45 - 25 * abs(math.sin(self._t * 0.07))),
            "odom_m": round(self.odom, 1),
        }, separators=(",", ":")).encode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    # No default: with --credentials the whole file is used unless you say
    # otherwise, and a silent truncation to 6 would look like robots missing.
    ap.add_argument("-n", "--count", type=int, default=None)
    ap.add_argument("--hz", type=float, default=2.0)
    ap.add_argument("--area", type=float, default=12.0, help="arena half-size in metres")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--credentials",
                    help="JSON from tools/provision_sim.py; one MQTT connection "
                         "per robot using its own enrolled credentials")
    args = ap.parse_args()

    # With the broker's topic ACL on, a single shared connection cannot publish
    # for the whole fleet -- each robot must speak as itself. Give every robot
    # its own client when credentials are supplied.
    creds = {}
    if args.credentials:
        with open(args.credentials, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        if not creds:
            print("credentials file is empty", file=sys.stderr)
            return 2
        ids = sorted(creds)[:args.count] if args.count else sorted(creds)
        fleet = []
        for idx, rid in enumerate(ids, start=1):
            r = SimRobot(idx, args.area)
            r.id, r.name = rid, rid.upper()
            fleet.append(r)
        host = creds[ids[0]].get("host", args.host)
        port = int(creds[ids[0]].get("port", args.port))
    else:
        fleet = [SimRobot(i + 1, args.area) for i in range(args.count or 6)]
        host, port = args.host, args.port

    clients = {}
    if creds:
        for r in fleet:
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=r.id)
            cl.username_pw_set(creds[r.id]["username"], creds[r.id]["password"])
            cl.will_set(f"fleet/{r.id}/lwt", "offline", qos=1, retain=True)
            cl.connect(host, port, keepalive=30)
            cl.loop_start()
            clients[r.id] = cl
        client = None
    else:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"sim-{random.randint(0,9999)}")
        if args.username:
            client.username_pw_set(args.username, args.password)
        client.connect(host, port, keepalive=30)
        client.loop_start()
        for r in fleet:
            clients[r.id] = client

    # announce presence; the broker fires the will if the sim dies hard
    for r in fleet:
        clients[r.id].publish(f"fleet/{r.id}/lwt", "online", qos=1, retain=True)

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    dt = 1.0 / args.hz
    sent = sent_bytes = 0
    t0 = last_report = time.time()
    mode = f"{len(clients)} authenticated connections" if creds else "one shared connection"
    print(f"publishing {len(fleet)} robots @ {args.hz} Hz -> {host}:{port}"
          f"  ({len(fleet) * args.hz:.0f} msg/s, {mode})   Ctrl-C to stop")

    while running:
        loop_start = time.time()
        for r in fleet:
            r.step(dt)
            data = r.payload()
            clients[r.id].publish(f"fleet/{r.id}/status", data, qos=0)
            sent += 1
            sent_bytes += len(data)

        now = time.time()
        if now - last_report >= 5:
            elapsed = now - t0
            print(f"  {sent:>7} msgs  {sent_bytes/1024:>9.1f} KiB  "
                  f"{sent_bytes/elapsed/1024:.1f} KiB/s uplink  "
                  f"avg {sent_bytes/max(sent,1):.0f} B/msg")
            last_report = now

        time.sleep(max(0.0, dt - (time.time() - loop_start)))

    for r in fleet:
        clients[r.id].publish(f"fleet/{r.id}/lwt", "offline", qos=1, retain=True)
    time.sleep(0.3)
    for cl in set(clients.values()):
        cl.loop_stop()
        cl.disconnect()
    print(f"\nstopped after {sent} messages / {sent_bytes/1024:.1f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
