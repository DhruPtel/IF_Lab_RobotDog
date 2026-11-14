#!/usr/bin/env python3
"""Smoke test, no LLM: 4 random moves, 1.5 s each, 1 s pause between.

  connect (UNITREE_MODE from .env, currently sta) -> obstacle avoidance ON
  (refuses to run if not confirmed) -> BalanceStand -> 4 moves -> StopMove.

ESC or Ctrl+C stops him and exits, same as the other scripts.
Backward is deliberately not in the pool: he can't see behind him.
"""
import asyncio
import random
import sys
import time

from robot import (
    ConnectFailed,
    QuitRequested,
    Robot,
    install_quit_handlers,
    read_keys,
    run_app,
)

SPEED = 0.3        # stick deflection on the avoidance (joystick) path
YAW = 0.6
MOVE_S = 1.5
PAUSE_S = 1.0
COUNT = 4
POOL = {
    "forward": (SPEED, 0, 0),
    "left": (0, SPEED, 0),
    "right": (0, -SPEED, 0),
    "turn_left": (0, 0, YAW),
    "turn_right": (0, 0, -YAW),
}


def say(line):
    t = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
    sys.stdout.write(f"\r\033[K{stamp}  {line}\n")
    sys.stdout.flush()


class FourMoves:
    def __init__(self):
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    async def hold(self, x, y, z, seconds):
        """Send (x, y, z) at the path's rate for `seconds`; False if quit."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.robot.drive(x, y, z)
            try:
                await asyncio.wait_for(self.quit.wait(), 1.0 / self.robot.rate_hz())
                return False
            except asyncio.TimeoutError:
                pass
        return True

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
                say(f"ABORT  obstacle avoidance not confirmed ON (code {code})")
                return 1
            say(f"AVOID ON  (path {self.robot.path_name()})")
            say(f"BalanceStand -> code {await self.robot.sport('BalanceStand')}")
            moves = random.choices(list(POOL), k=COUNT)
            say(f"Plan: {', '.join(moves)}")
            for i, name in enumerate(moves, 1):
                say(f"[{i}/{COUNT}] {name} for {MOVE_S}s")
                if not await self.hold(*POOL[name], MOVE_S):
                    break
                say(f"[{i}/{COUNT}] pause {PAUSE_S}s")
                if not await self.hold(0, 0, 0, PAUSE_S):
                    break
            else:
                say("Done")
                return 0
        except QuitRequested:
            pass
        except asyncio.TimeoutError:
            say("ABORT  robot stopped answering requests")
            return 1
        say("Stopped by ESC / Ctrl+C")
        return 0


async def amain():
    f = FourMoves()
    loop = asyncio.get_running_loop()
    install_quit_handlers(loop, f.quit)
    loop.add_reader(sys.stdin.fileno(), f.on_stdin)
    try:
        return await f.run()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        await f.robot.shutdown()


if __name__ == "__main__":
    run_app(amain)
