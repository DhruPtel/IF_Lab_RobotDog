#!/usr/bin/env python3
"""Keyboard teleop for the Go2 over LocalAP (laptop joined to GO2-xxxx wifi).

Keys:  W/S  forward/back     A/D  left/right     Q/E  turn left/right
       U  MoonWalk   I  Pose (toggle)   O  Dance1   B  obstacle avoidance on/off
       + / -  drive speed up/down    R  TrotRun on/off    L  cycle speed level
       SPACE  zero all (also leaves Pose)   ESC or Ctrl+C  StopMove and quit

Velocity goes out via robot.Robot.drive(): sport Move (api 1008) when
avoidance is off, through the obstacle-avoidance service when it is on.
"""
import asyncio
import json
import sys
import time

from robot import (
    SPORT_CMD,
    SPORT_CMD_MCF,
    ConnectFailed,
    QuitRequested,
    Robot,
    clamp,
    install_quit_handlers,
    read_keys,
    run_app,
)
import robot as robotmod

# ─── Configuration ───────────────────────────────────────────────────
SPEED = 0.3            # m/s for W/S and A/D (stick deflection on joystick path)
YAW = 0.6              # rad/s for Q/E
HOLD_TIMEOUT_S = 0.6   # axis returns to 0 if its key isn't seen this long
                       # (deadman; must exceed ~0.5 s keyboard repeat delay).
                       # None = latch until SPACE.
AVOID_AT_START = True  # obstacle avoidance ON at startup
SPEED_STEP = 0.1       # +/- change to SPEED per keypress
SPEED_MIN = 0.1        # floor for +/- (ceiling is robot.MAX_SPEED)
# L cycles SpeedLevel (api 1015, {"data": n}). -1/0/1 = slow/normal/fast in
# the Unitree SDK; the library doesn't document the values, so check codes.
SPEED_LEVELS = [-1, 0, 1]

# Trick keys -> exact SPORT_CMD names. A name missing from SPORT_CMD disables
# its key (nothing is substituted). Velocity streaming pauses during a trick so
# zero-velocity commands don't cut it short; any drive key or SPACE resumes.
ACTION_KEYS = {"u": "MoonWalk", "i": "Pose", "o": "Dance1"}
ACTION_PARAMS = {"Pose": {"data": True}}   # per MCF example; exit with StopMove
# ─────────────────────────────────────────────────────────────────────

KEYMAP = {  # key -> (axis, sign)
    "w": ("x", +1), "s": ("x", -1),
    "a": ("y", +1), "d": ("y", -1),
    "q": ("z", +1), "e": ("z", -1),
}


class Teleop:
    def __init__(self):
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.speed = clamp(abs(SPEED), robotmod.MAX_SPEED)
        self.yaw = clamp(abs(YAW), robotmod.MAX_YAW)
        self.cmd = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.last_seen = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.ready = False
        self.streaming = True
        self.posed = False
        self.action_task = None
        self.avoid_task = None
        self.setting_task = None
        self.trot = False           # assumed off at start (no readback API)
        self.level = None           # from GetSpeedLevel at start, if it answers
        self.last_line = None
        self.actions = {k: n for k, n in ACTION_KEYS.items() if n in SPORT_CMD}
        self.missing = {k: n for k, n in ACTION_KEYS.items() if n not in SPORT_CMD}

    # ── output ──────────────────────────────────────────────────────
    def notify(self, msg):
        sys.stdout.write("\r\033[K" + msg + "\n")
        sys.stdout.flush()
        self.last_line = None

    def avoid_label(self):
        s = self.robot.avoid_on
        return "AVOID ON" if s else ("AVOID OFF" if s is False else "AVOID ?")

    # ── keyboard ────────────────────────────────────────────────────
    def on_stdin(self):
        now = time.monotonic()
        for k in read_keys():
            if k in ("ESC", "CTRL_C", "EOF"):
                self.quit.set()
                return
            if not self.ready:
                continue
            if k == " ":
                self.cmd = {"x": 0.0, "y": 0.0, "z": 0.0}
                if self.posed:
                    self.start_action(next(c for c, n in self.actions.items() if n == "Pose"))
                else:
                    self.streaming = True
            elif k == "b":
                self.toggle_avoid()
            elif k in ("+", "=", "-", "_"):
                step = SPEED_STEP if k in ("+", "=") else -SPEED_STEP
                self.speed = round(min(max(self.speed + step, SPEED_MIN),
                                       robotmod.MAX_SPEED), 2)
                for axis in ("x", "y"):         # apply to a held key at once
                    if self.cmd[axis]:
                        self.cmd[axis] = self.speed if self.cmd[axis] > 0 else -self.speed
            elif k == "r":
                self.start_setting("TrotRun", self.toggle_trot)
            elif k == "l":
                self.start_setting("SpeedLevel", self.cycle_level)
            elif k in ACTION_KEYS:
                self.start_action(k)
            elif k in KEYMAP and self.posed:
                self.notify("In Pose - press i or SPACE to leave Pose before driving")
            elif k in KEYMAP:
                self.streaming = True
                axis, sign = KEYMAP[k]
                self.cmd[axis] = sign * (self.yaw if axis == "z" else self.speed)
                self.last_seen[axis] = now

    def apply_deadman(self):
        if HOLD_TIMEOUT_S is None:
            return
        now = time.monotonic()
        for axis in self.cmd:
            if self.cmd[axis] and now - self.last_seen[axis] > HOLD_TIMEOUT_S:
                self.cmd[axis] = 0.0

    # ── avoidance toggle ────────────────────────────────────────────
    def toggle_avoid(self):
        if self.avoid_task and not self.avoid_task.done():
            self.notify("[b] busy - avoidance switch still waiting for a reply")
            return
        target = not bool(self.robot.avoid_on)
        self.avoid_task = asyncio.ensure_future(self.switch_avoid(target))

    async def switch_avoid(self, enable):
        # Stop before changing the velocity path so nothing is left latched.
        self.cmd = {"x": 0.0, "y": 0.0, "z": 0.0}
        if self.robot.connected:
            self.robot.drive(0.0, 0.0, 0.0)
        try:
            state, code = await self.robot.set_avoid(enable)
        except QuitRequested:
            return
        except asyncio.TimeoutError:
            self.notify(f"Avoidance {'ON' if enable else 'OFF'} -> no reply; state unknown")
            self.robot.avoid_on = None
            return
        ok = state == enable
        self.notify(f"Avoidance {'ON' if enable else 'OFF'} -> code {code}, "
                    f"now {self.avoid_label()} (path: {self.robot.path_name()})"
                    + ("" if ok else "  ** did not take - see RUNBOOK **"))

    # ── run / speed level ───────────────────────────────────────────
    def start_setting(self, label, fn):
        if self.setting_task and not self.setting_task.done():
            self.notify(f"[{label}] busy - previous setting still waiting for a reply")
            return
        self.setting_task = asyncio.ensure_future(self.run_setting(label, fn))

    async def run_setting(self, label, fn):
        try:
            await fn()
        except QuitRequested:
            pass
        except asyncio.TimeoutError:
            self.notify(f"{label} -> no reply in {robotmod.REQUEST_TIMEOUT_S}s")

    async def toggle_trot(self):
        target = not self.trot
        code, _ = await self.robot.sport_mcf("TrotRun", {"data": target})
        if code == 0:
            self.trot = target
        self.notify(f"TrotRun {'on' if target else 'off'} (api {SPORT_CMD_MCF['TrotRun']}) -> code {code}"
                    + ("" if code == 0 else "  (rejected - likely not on this firmware/mode)"))

    async def cycle_level(self):
        cur = self.level if self.level in SPEED_LEVELS else SPEED_LEVELS[0]
        target = SPEED_LEVELS[(SPEED_LEVELS.index(cur) + 1) % len(SPEED_LEVELS)]
        code = await self.robot.sport("SpeedLevel", {"data": target})
        if code == 0:
            self.level = target
        self.notify(f"SpeedLevel {target} (api {SPORT_CMD['SpeedLevel']}) -> code {code}"
                    + ("" if code == 0 else "  (rejected)"))

    async def read_level(self):
        try:
            code, data = await self.robot.request(robotmod.SPORT_TOPIC,
                                                  SPORT_CMD["GetSpeedLevel"], timeout=3)
            if code == 0 and data:
                self.level = json.loads(data).get("data")
        except QuitRequested:
            raise
        except Exception:
            self.level = None

    # ── tricks ──────────────────────────────────────────────────────
    def start_action(self, key):
        if key in self.missing:
            self.notify(f"[{key}] {self.missing[key]} is not in SPORT_CMD - key disabled")
            return
        if self.action_task and not self.action_task.done():
            self.notify(f"[{key}] busy - previous command still waiting for a reply")
            return
        name = self.actions[key]
        if name == "Pose" and self.posed:
            label, call, param = "Pose off (StopMove)", "StopMove", None
        else:
            label, call, param = name, name, ACTION_PARAMS.get(name)
        self.cmd = {"x": 0.0, "y": 0.0, "z": 0.0}
        self.streaming = False
        self.action_task = asyncio.ensure_future(self.run_action(name, label, call, param))

    async def run_action(self, name, label, call, param):
        self.notify(f"{label} (api {SPORT_CMD[call]}) sent...")
        try:
            code = await self.robot.sport(call, param)
        except QuitRequested:
            return
        except asyncio.TimeoutError:
            self.notify(f"{label} -> no reply in {robotmod.REQUEST_TIMEOUT_S}s")
            return
        self.notify(f"{label} -> code {code}" + ("" if code == 0 else
                    "  (non-zero: rejected - likely not supported in this mode/firmware)"))
        if name == "Pose" and code == 0:
            self.posed = call != "StopMove"
            if not self.posed:
                self.streaming = True

    # ── main ────────────────────────────────────────────────────────
    async def drive_loop(self):
        while not self.quit.is_set():
            self.apply_deadman()
            x, y, z = self.cmd["x"], self.cmd["y"], self.cmd["z"]
            if self.streaming:
                self.robot.drive(x, y, z)
                motion = f"x={x:+.2f}  y={y:+.2f}  yaw={z:+.2f}"
            else:
                motion = ("POSE - i/SPACE to exit" if self.posed
                          else "TRICK - drive paused; drive key or SPACE resumes")
            lvl = "?" if self.level is None else self.level
            line = (f"[{self.avoid_label()} | spd {self.speed:.1f} | lvl {lvl} | "
                    f"RUN {'on' if self.trot else 'off'}] {motion}")
            if line != self.last_line:
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
                self.last_line = line
            try:
                await asyncio.wait_for(self.quit.wait(), 1.0 / self.robot.rate_hz())
            except asyncio.TimeoutError:
                pass

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
            mode = await self.robot.motion_mode()
            print(f"Motion mode: {mode or '(no answer - normal on newer MCF firmware)'}")
            if AVOID_AT_START:
                await self.switch_avoid(True)
            else:
                try:
                    self.robot.avoid_on = await self.robot.get_avoid()
                except asyncio.TimeoutError:
                    self.robot.avoid_on = None
            try:
                code = await self.robot.sport("BalanceStand")
                print(f"BalanceStand -> code {code}" + ("" if code == 0 else "  (non-zero: see RUNBOOK)"))
            except asyncio.TimeoutError:
                print("BalanceStand -> no reply (continuing; see RUNBOOK)")
            await self.read_level()
            print(f"Speed level: {self.level if self.level is not None else '(no answer)'}")
        except QuitRequested:
            return 0

        self.ready = True
        for k, n in self.missing.items():
            print(f"Key {k.upper()}: {n} not found in SPORT_CMD - disabled")
        tricks = "  ".join(f"{k.upper()}={n}" for k, n in self.actions.items())
        print(f"\n>>> READY - W/S A/D Q/E drive, SPACE stop, B avoidance, +/- speed, "
              f"R run, L level, ESC quit, {tricks} <<<")
        await self.drive_loop()
        return 0


async def amain():
    t = Teleop()
    loop = asyncio.get_running_loop()
    install_quit_handlers(loop, t.quit)
    loop.add_reader(sys.stdin.fileno(), t.on_stdin)
    try:
        return await t.run()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        for task in (t.action_task, t.avoid_task, t.setting_task):
            if task and not task.done():
                task.cancel()
        await t.robot.shutdown()


if __name__ == "__main__":
    run_app(amain)
