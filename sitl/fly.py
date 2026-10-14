#!/usr/bin/env python3
"""Fly one SITL run with a given parameter file.

The run is what an operator does every day with a ground station: take off,
click "fly here" on a point 300 m away, and wait there. Then the radio link
drops, the way it does when a vehicle goes behind a building or out of range.
This script plays the ground station: it applies the parameter file, flies
the vehicle out, and at the chosen moment stops sending its heartbeats while
it keeps listening, so the telemetry after the link loss is still recorded.

    fly.py --params FILE --log run.csv --events run.json

What the vehicle does next is decided by its parameters, which is the point.
"""

import argparse
import csv
import json
import math
import sys
import threading
import time

from pymavlink import mavutil

sys.path.insert(0, "/repo/src")
from paramguard import params as pfile  # noqa: E402

TARGET_NORTH_M = 300.0
ALT_M = 30.0
ARRIVED_M = 5.0
OBSERVE_AFTER_LOSS_S = 210.0
EARTH_R = 6378137.0


class Ground:
    def __init__(self, url):
        self.m = mavutil.mavlink_connection(url, source_system=255, source_component=190)
        self.link_up = True
        self.lock = threading.Lock()
        self.stop = False
        threading.Thread(target=self._heartbeat, daemon=True).start()

    def _heartbeat(self):
        while not self.stop:
            if self.link_up:
                with self.lock:
                    self.m.mav.heartbeat_send(
                        mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0
                    )
            time.sleep(0.5)

    def send(self, fn, *args):
        with self.lock:
            fn(*args)

    def command(self, cmd, *p, timeout=10.0):
        p = list(p) + [0] * (7 - len(p))
        self.send(self.m.mav.command_long_send, self.m.target_system, self.m.target_component, cmd, 0, *p)
        deadline = time.time() + timeout
        while time.time() < deadline:
            ack = self.m.recv_match(type="COMMAND_ACK", blocking=True, timeout=1)
            if ack and ack.command == cmd:
                return ack.result
        return None


def apply_params(g, path):
    """Set every parameter in the file and read each one back."""
    f = pfile.load(path)
    applied = {}
    for name in f.names():
        p = f[name]
        ptype = (
            mavutil.mavlink.MAV_PARAM_TYPE_INT32 if p.declared_type == "int" else mavutil.mavlink.MAV_PARAM_TYPE_REAL32
        )
        for _ in range(5):
            if ptype == mavutil.mavlink.MAV_PARAM_TYPE_INT32:
                # PX4 carries int32 values bit-for-bit in the float field.
                import struct

                wire = struct.unpack("f", struct.pack("i", int(p.value)))[0]
            else:
                wire = p.value
            g.send(g.m.mav.param_set_send, g.m.target_system, g.m.target_component, name.encode(), wire, ptype)
            msg = g.m.recv_match(type="PARAM_VALUE", blocking=True, timeout=2)
            while msg and msg.param_id != name:
                msg = g.m.recv_match(type="PARAM_VALUE", blocking=True, timeout=2)
            if msg:
                applied[name] = p.text
                break
        else:
            raise SystemExit(f"PX4 did not confirm {name}")
    return applied


def read_param(g, name):
    g.send(g.m.mav.param_request_read_send, g.m.target_system, g.m.target_component, name.encode(), -1)
    deadline = time.time() + 5
    while time.time() < deadline:
        msg = g.m.recv_match(type="PARAM_VALUE", blocking=True, timeout=1)
        if msg and msg.param_id == name:
            if msg.param_type == mavutil.mavlink.MAV_PARAM_TYPE_INT32:
                import struct

                return struct.unpack("i", struct.pack("f", msg.param_value))[0]
            return msg.param_value
    return None


def offset(lat, lon, north, east):
    dlat = north / EARTH_R
    dlon = east / (EARTH_R * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="udpin:0.0.0.0:14540")
    ap.add_argument("--params", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--events", required=True)
    args = ap.parse_args()

    g = Ground(args.url)
    g.m.wait_heartbeat(timeout=60)
    print(f"connected to system {g.m.target_system}", flush=True)
    events = []
    t0 = time.time()

    def event(text):
        events.append({"t": round(time.time() - t0, 2), "text": text})
        print(f"[{time.time() - t0:7.1f}] {text}", flush=True)

    applied = apply_params(g, args.params)
    event(f"applied {len(applied)} parameters from {args.params.split('/')[-1]}")
    watched = {n: read_param(g, n) for n in ("NAV_DLL_ACT", "COM_DL_LOSS_T", "GF_ACTION")}
    event("on the vehicle: " + ", ".join(f"{k}={v}" for k, v in watched.items()))

    # Home is the first global position once the estimator has one.
    home = None
    while home is None:
        msg = g.m.recv_match(type="HOME_POSITION", blocking=True, timeout=2)
        if msg:
            home = (msg.latitude / 1e7, msg.longitude / 1e7, msg.altitude / 1e3)
        else:
            g.command(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, mavutil.mavlink.MAVLINK_MSG_ID_HOME_POSITION)

    # Wait until the commander is happy to arm (pre-flight checks pass).
    for _ in range(60):
        if g.command(mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 1) == 0:
            break
        time.sleep(2)
    else:
        raise SystemExit("could not arm")
    event("armed")
    g.command(mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, -1, 0, 0, math.nan, math.nan, math.nan, home[2] + ALT_M)
    event(f"takeoff to {ALT_M:.0f} m")

    target = offset(home[0], home[1], TARGET_NORTH_M, 0)
    phase = "takeoff"
    lost_at = None
    landed_at = None
    rows = []
    pos = None
    last_mode = None
    deadline = time.time() + 400
    with open(args.log, "w", newline="") as logf:
        w = csv.writer(logf)
        w.writerow(["t", "north", "east", "up", "mode", "armed", "link"])
        while time.time() < deadline:
            msg = g.m.recv_match(type=["LOCAL_POSITION_NED", "HEARTBEAT", "STATUSTEXT"], blocking=True, timeout=1)
            if msg is None:
                continue
            kind = msg.get_type()
            if kind == "STATUSTEXT":
                event(f"PX4: {msg.text}")
                continue
            if kind == "HEARTBEAT":
                if msg.get_srcSystem() != g.m.target_system or msg.type == mavutil.mavlink.MAV_TYPE_GCS:
                    continue
                mode = g.m.flightmode
                if mode != last_mode:
                    event(f"mode {mode}")
                    last_mode = mode
                continue
            pos = (msg.x, msg.y, -msg.z)
            t = time.time() - t0
            armed = bool(g.m.motors_armed())
            row = [
                round(t, 2),
                round(pos[0], 2),
                round(pos[1], 2),
                round(pos[2], 2),
                last_mode,
                int(armed),
                int(g.link_up),
            ]
            if not rows or t - rows[-1][0] >= 0.2:
                rows.append(row)
                w.writerow(row)
            if phase == "takeoff" and pos[2] > ALT_M - 2:
                g.command(
                    mavutil.mavlink.MAV_CMD_DO_REPOSITION,
                    -1,
                    1,
                    0,
                    math.nan,
                    int(target[0] * 1e7) / 1e7,
                    int(target[1] * 1e7) / 1e7,
                    home[2] + ALT_M,
                )
                event(f"ground station: fly to {TARGET_NORTH_M:.0f} m north")
                phase = "outbound"
            elif phase == "outbound" and math.hypot(pos[0] - TARGET_NORTH_M, pos[1]) < ARRIVED_M:
                event("arrived; holding")
                phase = "holding"
                hold_since = time.time()
            elif phase == "holding" and time.time() - hold_since > 5:
                g.link_up = False
                lost_at = time.time()
                event("radio link lost (ground station heartbeats stop)")
                phase = "lost"
            elif phase == "lost":
                if not armed and landed_at is None:
                    landed_at = time.time()
                    event("disarmed")
                if time.time() - lost_at > OBSERVE_AFTER_LOSS_S or (landed_at and time.time() - landed_at > 3):
                    break
    g.stop = True
    if lost_at is None:
        raise SystemExit("the run never reached the link-loss point")
    json.dump(
        {
            "params": args.params.split("/")[-1],
            "applied": applied,
            "on_vehicle": watched,
            "link_lost_t": round(lost_at - t0, 2),
            "events": events,
        },
        open(args.events, "w"),
        indent=1,
    )
    final = rows[-1]
    event(f"end: {math.hypot(final[1], final[2]):.0f} m from home, {final[3]:.0f} m up, mode {final[4]}")


if __name__ == "__main__":
    main()
