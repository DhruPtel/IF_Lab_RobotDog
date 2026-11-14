#!/usr/bin/env python3
"""Walk to the end of the hallway, turn around, come back. Proof of concept.

  setup     avoidance ON (won't start otherwise), BalanceStand, record the
            starting IMU yaw (= hallway heading) and robot_pose (= home)
  OUTBOUND  per burst: 1 frame -> 1 claude-haiku-5-5 call -> forward /
            adjust_left / adjust_right (~15 deg turn, then walk) / at_end;
            then turn back if > 10 deg off the hallway heading.
            Ends on at_end or after 15 bursts.
  TURN      180 deg on the IMU
  RETURN    same, also stops within 0.5 m of home.

Bad JSON -> forward 1.5 s, logged FALLBACK. walk_s clamped 1-3.
Log: runs/<ts>-hallway.jsonl. ESC or Ctrl+C stops him and exits.
"""
import asyncio
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

from llm import ApiFatal, describe, loud, make_client, preflight
from nav import Nav, wrap180
from robot import (
    ConnectFailed,
    QuitRequested,
    Robot,
    install_quit_handlers,
    read_keys,
    run_app,
)

# ─── Configuration ───────────────────────────────────────────────────
MODEL = "claude-haiku-5-5"
WALK_SPEED = 0.3          # forward stick deflection (avoidance path)
WALK_LIMITS = (1, 3)
FALLBACK_WALK_S = 1.5
ADJUST_DEG = 15
DRIFT_DEG = 10
MAX_BURSTS = 15
HOME_RADIUS_M = 0.5
REQUIRE_CAMERA = True     # refuse to run if no video frames after warm-up
# ─────────────────────────────────────────────────────────────────────

PROMPT = (
    "You are a robot dog walking down a hallway. This is what you see. "
    "Are you at the end of the hallway, or is there more open floor ahead? "
    "Respond with JSON only: "
    '{"action": "forward" | "adjust_left" | "adjust_right" | "at_end", '
    '"walk_s": 1-3, "reason": "one sentence"}'
)
ACTIONS = ("forward", "adjust_left", "adjust_right", "at_end")


def stamp(t=None):
    return datetime.fromtimestamp(t or time.time()).isoformat(timespec="milliseconds")


def say(line):
    t = time.time()
    s = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
    sys.stdout.write(f"\r\033[K{s}  {line}\n")
    sys.stdout.flush()


def parse_json(text):
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return json.loads(s)


class Hallway:
    def __init__(self):
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.nav = Nav(self.robot, self.quit)
        self.client = make_client()
        self.home = None
        Path("runs").mkdir(exist_ok=True)
        self.log_path = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-hallway.jsonl")

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    def dist_home(self):
        p = self.nav.position()
        if p is None or self.home is None:
            return None
        return math.hypot(p[0] - self.home[0], p[1] - self.home[1])

    async def decide(self):
        jpeg, frame_t = self.nav.jpeg_b64(), self.nav.frame_t
        content = [{"type": "text", "text": "(no camera frame available)"}] if jpeg is None else [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg}}]
        content.append({"type": "text", "text": PROMPT})
        response = await self.robot.until_quit(self.client.messages.create(
            model=MODEL,
            max_tokens=2048,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        ), timeout=None)
        raw = next((b.text for b in response.content if b.type == "text"), "")
        return raw, response.stop_reason, frame_t

    async def leg(self, name, target, check_home):
        """Run one leg toward `target` heading. Returns why it ended."""
        for n in range(1, MAX_BURSTS + 1):
            raw, stop, frame_t, fallback = "", None, None, None
            try:
                raw, stop, frame_t = await self.decide()
                d = parse_json(raw)
                action = str(d["action"])
                walk_s = float(d["walk_s"])
                reason = str(d.get("reason", ""))
                if action not in ACTIONS:
                    raise ValueError(f"unknown action {action!r}")
            except (QuitRequested, ApiFatal):
                raise
            except anthropic.APIError as e:
                raise ApiFatal(describe(e)) from e
            except (ValueError, KeyError, TypeError) as e:
                fallback = f"{type(e).__name__}: {e}"[:200]
                action, walk_s, reason = "forward", FALLBACK_WALK_S, "fallback"
            walk_s = min(max(walk_s, WALK_LIMITS[0]), WALK_LIMITS[1])

            adjust, drift_fix = 0.0, 0.0
            if action == "adjust_left":
                adjust = await self.nav.turn_by(-ADJUST_DEG)
            elif action == "adjust_right":
                adjust = await self.nav.turn_by(ADJUST_DEG)
            if action != "at_end":
                await self.nav.drive_for(WALK_SPEED, 0, walk_s)
                off = wrap180(self.nav.heading() - target)
                if abs(off) > DRIFT_DEG:
                    drift_fix = await self.nav.turn_to(target)

            dist = self.dist_home()
            pos = self.nav.position()
            dist_s = f"{dist:.2f}m" if dist is not None else "?"
            say(f"{name} #{n:<2} {action:<12} {0 if action == 'at_end' else walk_s:.1f}s  "
                f"home {dist_s:>6}  {reason}" + (f"  [FALLBACK: {fallback}]" if fallback else ""))
            with self.log_path.open("a") as f:
                f.write(json.dumps({
                    "t": stamp(), "leg": name, "burst": n, "model": MODEL,
                    "frame_t": stamp(frame_t) if frame_t else None,
                    "raw": raw, "stop_reason": stop, "fallback": fallback,
                    "executed": {"action": action,
                                 "walk_s": 0 if action == "at_end" else round(walk_s, 2),
                                 "adjust_deg": round(adjust, 1),
                                 "drift_fix_deg": round(drift_fix, 1)},
                    "heading_deg": round(self.nav.heading(), 1),
                    "yaw_rad": self.nav.yaw,
                    "pose": pos, "dist_home_m": None if dist is None else round(dist, 3),
                }) + "\n")

            if action == "at_end":
                return "model said at_end"
            if check_home and dist is not None and dist <= HOME_RADIUS_M:
                return f"within {HOME_RADIUS_M} m of home"
        return f"{MAX_BURSTS} bursts"

    async def run(self):
        try:
            say(f"API preflight ({MODEL}) ...")
            await self.robot.until_quit(preflight(self.client, MODEL), timeout=None)
            say("API OK")
            await self.robot.connect()
        except ApiFatal as e:
            print(loud(str(e)))
            return 2
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
            say(await self.nav.start(video=True))
            if REQUIRE_CAMERA and not self.nav.camera_ok():
                say("ABORT  camera is not delivering frames - refusing to run blind "
                    "(power-cycle the dog; set REQUIRE_CAMERA = False to override)")
                return 1
            self.home = self.nav.position()
            say(f"HOME {self.home}  hallway heading = startup yaw "
                f"({self.nav.yaw0 if self.nav.yaw0 is not None else 'no IMU'})  log {self.log_path}")
            print(">>> HALLWAY - ESC or Ctrl+C to stop <<<")

            why = await self.leg("OUT", 0, check_home=False)
            say(f"OUT done: {why}")
            turned = await self.nav.turn_to(180)
            say(f"TURN  {turned:.0f} deg, heading now {self.nav.heading():.0f}")
            why = await self.leg("RET", 180, check_home=True)
            d = self.dist_home()
            say(f"RET done: {why}  (home {f'{d:.2f} m' if d is not None else '?'})")
        except QuitRequested:
            say("Stopped by ESC / Ctrl+C")
        except ApiFatal as e:
            print(loud(str(e)))
            return 2
        except asyncio.TimeoutError:
            say("ABORT  robot stopped answering requests")
            return 1
        return 0


async def amain():
    h = Hallway()
    loop = asyncio.get_running_loop()
    install_quit_handlers(loop, h.quit)
    loop.add_reader(sys.stdin.fileno(), h.on_stdin)
    try:
        return await h.run()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        await h.robot.shutdown()


if __name__ == "__main__":
    run_app(amain)
