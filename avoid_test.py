#!/usr/bin/env python3
"""Does obstacle avoidance clamp motion? Measure it in one clear spot.

Four 1.5 s forward trials, turning 180 deg between them so he walks back and
forth over the same floor. Distance is measured from rt/utlidar/robot_pose.

  A  avoidance OFF  sport Move 1008, x=0.3 m/s        baseline in real m/s
  B  avoidance OFF  joystick, stick 0.3               is 0.3 stick slow on its own?
  C  avoidance ON   joystick, stick 0.3               what agent.py does today
  D  avoidance ON   avoidance-API Move 1003, x=0.3    AVOID_DRIVE_PATH = "api"

No LLM, no camera. ESC or Ctrl+C stops him. Leaves avoidance ON at the end.
Results print as a table and go to runs/<ts>-avoidtest.jsonl.
"""
import asyncio
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import robot as robotmod
from nav import Nav
from robot import (
    JOY_TOPIC,
    ConnectFailed,
    QuitRequested,
    Robot,
    install_quit_handlers,
    read_keys,
    run_app,
)

TRIAL_S = 1.5
VALUE = 0.3
SETTLE_S = 1.0


class AvoidTest:
    def __init__(self):
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.nav = Nav(self.robot, self.quit)
        Path("runs").mkdir(exist_ok=True)
        self.log_path = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-avoidtest.jsonl")
        self.results = []

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    def say(self, line):
        sys.stdout.write(f"\r\033[K{time.strftime('%H:%M:%S')}  {line}\n")
        sys.stdout.flush()

    # explicit senders, so each trial uses exactly one path regardless of state
    def send_sport(self, x):
        self.robot._noreply(robotmod.SPORT_TOPIC, robotmod.SPORT_CMD["Move"], {"x": x, "y": 0, "z": 0})

    def send_joy(self, x):
        self.robot.conn.datachannel.pub_sub.publish_without_callback(
            JOY_TOPIC, {"lx": 0.0, "ly": x, "rx": 0.0, "ry": 0.0, "keys": 0})

    def send_avoid_api(self, x):
        self.robot._noreply(robotmod.AVOID_TOPIC, robotmod.OBSTACLES_AVOID_API["MOVE"],
                            {"x": x, "y": 0, "yaw": 0, "mode": 0})

    async def set_avoid(self, on):
        state, code = await self.robot.set_avoid(on)
        self.say(f"avoidance -> {state} (code {code})")
        return state

    async def trial(self, label, desc, sender, hz):
        await self.nav.pause(SETTLE_S)
        p0, h0 = self.nav.position(), self.nav.heading()
        t0 = time.monotonic()
        while time.monotonic() - t0 < TRIAL_S:
            sender(VALUE)
            await self.nav.pause(1.0 / hz)
        for _ in range(3):
            sender(0.0)
            await self.nav.pause(0.05)
        await self.nav.pause(SETTLE_S)          # let him stop before measuring
        p1, h1 = self.nav.position(), self.nav.heading()
        dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1]) if p0 and p1 else None
        row = {"trial": label, "desc": desc, "seconds": TRIAL_S, "value": VALUE,
               "dist_m": None if dist is None else round(dist, 3),
               "speed_mps": None if dist is None else round(dist / TRIAL_S, 3),
               "yaw_change_deg": round(((h1 - h0 + 180) % 360) - 180, 1),
               "avoid_on": self.robot.avoid_on, "p0": p0, "p1": p1}
        self.results.append(row)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"t": datetime.now().isoformat(timespec="milliseconds"), **row}) + "\n")
        self.say(f"{label}: {desc:<44} moved {row['dist_m']} m -> {row['speed_mps']} m/s")

    async def about_face(self):
        await self.nav.pause(0.5)
        turned = await self.nav.turn_by(180)
        self.say(f"   turned {turned:.0f} deg")

    async def run(self):
        try:
            await self.robot.connect()
        except QuitRequested:
            print("Aborted before connecting.")
            return 1
        except ConnectFailed as e:
            print(e)
            return 1
        try:
            self.say(f"BalanceStand -> code {await self.robot.sport('BalanceStand')}")
            self.say(await self.nav.start(video=False))
            if self.nav.position() is None:
                self.say("ABORT  no odometry - can't measure distance")
                return 1
            print(">>> AVOIDANCE TEST - 4 short forward trials, ESC to stop <<<")

            await self.set_avoid(False)
            await self.trial("A", "avoid OFF, sport Move x=0.3 m/s", self.send_sport, robotmod.SPORT_RATE_HZ)
            await self.about_face()
            await self.trial("B", "avoid OFF, joystick stick 0.3", self.send_joy, robotmod.JOYSTICK_RATE_HZ)
            await self.about_face()

            if await self.set_avoid(True) is not True:
                self.say("avoidance did not confirm ON - trials C/D skipped")
            else:
                await self.trial("C", "avoid ON, joystick stick 0.3", self.send_joy, robotmod.JOYSTICK_RATE_HZ)
                await self.about_face()
                await self.robot.set_api_control(True)
                self.say(f"   USE_REMOTE_COMMAND_FROM_API handover -> {self.robot.api_control}")
                await self.trial("D", "avoid ON, avoid-API Move 1003 x=0.3", self.send_avoid_api, robotmod.SPORT_RATE_HZ)
                await self.robot.set_api_control(False)

            print("\n=== Results (1.5 s each, same spot) ===")
            for r in self.results:
                print(f"  {r['trial']}  {r['desc']:<44} {r['dist_m']} m   {r['speed_mps']} m/s")
            print(f"Log: {self.log_path}")
        except QuitRequested:
            self.say("Stopped by ESC / Ctrl+C")
        except asyncio.TimeoutError:
            self.say("ABORT  robot stopped answering requests")
            return 1
        finally:
            try:
                await self.robot.set_api_control(False)
                if self.robot.connected and self.robot.avoid_on is not True:
                    await self.robot.set_avoid(True)            # leave it ON
            except Exception:
                pass
        return 0


async def amain():
    a = AvoidTest()
    loop = asyncio.get_running_loop()
    install_quit_handlers(loop, a.quit)
    loop.add_reader(sys.stdin.fileno(), a.on_stdin)
    try:
        return await a.run()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        await a.robot.shutdown()


if __name__ == "__main__":
    run_app(amain)
