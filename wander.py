#!/usr/bin/env python3
"""Dumb autonomous wander. Obstacle avoidance carries the safety.

  connect -> obstacle avoidance ON (refuses to run if it can't confirm ON)
  -> BalanceStand -> walk forward; every few seconds, or when forward
  progress stalls, turn a random amount and carry on.

ESC or Ctrl+C: StopMove and clean exit. Every decision prints one line:
  HH:MM:SS.mmm  ACTION        reason
"""
import asyncio
import math
import random
import sys
import time

from robot import (
    RTC_TOPIC,
    ConnectFailed,
    QuitRequested,
    Robot,
    install_quit_handlers,
    read_keys,
    run_app,
)

# ─── Configuration ───────────────────────────────────────────────────
FORWARD = 0.3              # m/s (stick deflection on the joystick path)
TURN_YAW = 0.6             # rad/s while turning
LEG_S = (4.0, 8.0)         # walk this long (random in range) before a turn
TURN_DEG = (45, 180)       # random turn magnitude, random direction
STALL_WINDOW_S = 2.0       # look at progress over this window...
STALL_MIN_M = 0.10         # ...stalled if moved less than this
STALL_GRACE_S = 1.5        # ignore stalls this soon after starting a leg
STATE_STALE_S = 1.0        # odometry older than this = unavailable
# ─────────────────────────────────────────────────────────────────────

STATE_TOPIC = RTC_TOPIC["LF_SPORT_MOD_STATE"]   # as in sportmodestate.py


def log(action, reason):
    t = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
    sys.stdout.write(f"\r\033[K{stamp}  {action:<12}  {reason}\n")
    sys.stdout.flush()


class Wander:
    def __init__(self):
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.pos = None             # (x, y) from sportmodestate
        self.pos_t = 0.0
        self.track = []             # [(t, x, y)] during the current leg
        self.odom_warned = False

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    def on_state(self, message):
        try:
            p = message["data"]["position"]
            self.pos, self.pos_t = (float(p[0]), float(p[1])), time.monotonic()
        except Exception:
            pass

    def odom_ok(self):
        return self.pos is not None and time.monotonic() - self.pos_t < STATE_STALE_S

    async def tick(self, x, z, seconds):
        """Command (x, z) for `seconds`; returns early on quit."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.quit.is_set():
            self.robot.drive(x, 0.0, z)
            try:
                await asyncio.wait_for(self.quit.wait(), 1.0 / self.robot.rate_hz())
            except asyncio.TimeoutError:
                pass

    def stalled(self, leg_start):
        now = time.monotonic()
        if not self.odom_ok():
            if not self.odom_warned:
                log("NO-ODOM", f"no fresh {STATE_TOPIC} position - stall detection off, timer only")
                self.odom_warned = True
            return None
        if self.odom_warned:
            log("ODOM-OK", "position updates resumed - stall detection back on")
            self.odom_warned = False
        self.track.append((now, *self.pos))
        self.track = [p for p in self.track if now - p[0] <= STALL_WINDOW_S + 0.5]
        if now - leg_start < max(STALL_GRACE_S, STALL_WINDOW_S):
            return None
        old = next((p for p in self.track if now - p[0] >= STALL_WINDOW_S), None)
        if old is None:
            return None
        dist = math.hypot(self.pos[0] - old[1], self.pos[1] - old[2])
        return dist if dist < STALL_MIN_M else None

    async def walk_leg(self):
        leg = random.uniform(*LEG_S)
        log("FORWARD", f"x={FORWARD} for up to {leg:.1f}s")
        start = time.monotonic()
        self.track = []
        while not self.quit.is_set():
            elapsed = time.monotonic() - start
            if elapsed >= leg:
                return f"leg timer ({leg:.1f}s) done"
            await self.tick(FORWARD, 0.0, 0.1)
            d = self.stalled(start)
            if d is not None:
                return f"stalled: moved {d:.2f}m in {STALL_WINDOW_S:.0f}s (avoidance likely holding him)"
        return None

    async def turn(self, reason):
        deg = random.uniform(*TURN_DEG)
        sign = random.choice((+1, -1))
        secs = math.radians(deg) / TURN_YAW
        log("TURN-" + ("LEFT" if sign > 0 else "RIGHT"), f"{deg:.0f} deg ({secs:.1f}s) - {reason}")
        await self.tick(0.0, sign * TURN_YAW, secs)

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
            state, code = await self.robot.set_avoid(True)
            if state is not True:
                log("ABORT", f"obstacle avoidance not confirmed ON (code {code}, state {state}) - "
                             "refusing to wander without it")
                return 1
            log("AVOID-ON", f"confirmed (code {code}, path {self.robot.path_name()})")
            code = await self.robot.sport("BalanceStand")
            log("STAND", f"BalanceStand -> code {code}")
            self.robot.subscribe(STATE_TOPIC, self.on_state)
            print(">>> WANDERING - ESC or Ctrl+C to stop <<<")
            while not self.quit.is_set():
                reason = await self.walk_leg()
                if reason is None:
                    break
                await self.turn(reason)
        except QuitRequested:
            pass
        except asyncio.TimeoutError:
            log("ABORT", "robot stopped answering requests")
            return 1
        log("QUIT", "ESC / Ctrl+C")
        return 0


async def amain():
    w = Wander()
    loop = asyncio.get_running_loop()
    install_quit_handlers(loop, w.quit)
    loop.add_reader(sys.stdin.fileno(), w.on_stdin)
    try:
        return await w.run()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        await w.robot.shutdown()


if __name__ == "__main__":
    run_app(amain)
