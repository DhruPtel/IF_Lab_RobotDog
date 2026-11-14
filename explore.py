#!/usr/bin/env python3
"""Look around, pick a spot, walk there - three times. Proof of concept.

Each iteration:
  SCAN    face heading 0, then turn right in 90 deg steps: frames at 0/90/180/270
  DECIDE  one claude-haiku-5-5 call with the 4 labelled frames -> JSON heading
  MOVE    turn to that heading, walk forward walk_s (clamped 2-5 s)
  LOG     frame timestamps + raw reply + executed move -> runs/<ts>-explore.jsonl

Headings are degrees clockwise from the way he faced at startup (nav.py), so
they mean the same thing every iteration. Turns are closed-loop on IMU yaw,
timed (uncalibrated) if no IMU arrives.

Bad JSON / refusal / API error -> random heading, logged as a fallback. It
always moves. ESC or Ctrl+C stops him and exits. Obstacle avoidance must
confirm ON or it won't start.
"""
import asyncio
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

from llm import ApiFatal, describe, loud, make_client, preflight
from nav import Nav
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
ITERATIONS = 3
WALK_SPEED = 0.3          # forward stick deflection (avoidance path)
WALK_LIMITS = (2, 5)      # walk_s clamp
SETTLE_S = 0.6            # pause before grabbing a frame
HEADINGS = (0, 90, 180, 270)
REQUIRE_CAMERA = True     # refuse to run if no video frames after warm-up
# ─────────────────────────────────────────────────────────────────────

PROMPT = (
    "You are a robot dog deciding where to walk next. These four images are "
    "what you see looking in four directions. Pick the direction with the "
    "most open floor to walk into. You must pick a direction and you must "
    "walk — staying put is not allowed. Respond with JSON only: "
    '{"heading": 0|90|180|270, "walk_s": 2-5, "reason": "one sentence"}'
)


def stamp():
    return datetime.now().isoformat(timespec="milliseconds")


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


class Explore:
    def __init__(self):
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.nav = Nav(self.robot, self.quit)
        self.client = make_client()
        self.visited = []
        Path("runs").mkdir(exist_ok=True)
        self.log_path = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-explore.jsonl")

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    async def scan(self):
        shots = []
        for h in HEADINGS:
            await self.nav.turn_to(h)
            await self.nav.pause(SETTLE_S)
            jpeg = self.nav.jpeg_b64()
            shots.append({"heading": h, "t": stamp(),
                          "actual_heading": round(self.nav.heading(), 1), "jpeg": jpeg})
            say(f"   scan {h:>3}: {'frame' if jpeg else 'NO FRAME'} (at {self.nav.heading():.0f} deg)")
        return shots

    async def decide(self, shots):
        content = []
        for s in shots:
            if s["jpeg"]:
                content.append({"type": "text", "text": f"Heading {s['heading']}:"})
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": s["jpeg"]}})
            else:
                content.append({"type": "text", "text": f"Heading {s['heading']}: (no image)"})
        text = PROMPT
        if self.visited:
            text += ("\n\nHeadings you already walked toward this run, oldest first "
                     f"(same compass as the images): {', '.join(map(str, self.visited))}. "
                     "Walking toward the opposite of your last heading takes you back "
                     "where you came from.")
        content.append({"type": "text", "text": text})
        response = await self.robot.until_quit(self.client.messages.create(
            model=MODEL,
            max_tokens=2048,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        ), timeout=None)
        raw = next((b.text for b in response.content if b.type == "text"), "")
        return raw, response.stop_reason

    async def iteration(self, n):
        say(f"[{n}/{ITERATIONS}] SCAN")
        shots = await self.scan()
        say(f"[{n}/{ITERATIONS}] DECIDE ({MODEL})")
        raw, stop, fallback = "", None, None
        try:
            raw, stop = await self.decide(shots)
            d = parse_json(raw)
            heading = int(d["heading"])
            walk_s = float(d["walk_s"])
            reason = str(d.get("reason", ""))
            if heading not in HEADINGS:
                raise ValueError(f"heading {heading} not one of {HEADINGS}")
        except (QuitRequested, ApiFatal):
            raise
        except anthropic.APIError as e:
            raise ApiFatal(describe(e)) from e
        except (ValueError, KeyError, TypeError) as e:
            fallback = f"{type(e).__name__}: {e}"[:200]
            heading = random.choice(HEADINGS)
            walk_s = random.uniform(*WALK_LIMITS)
            reason = "fallback - random heading"
        walk_s = min(max(walk_s, WALK_LIMITS[0]), WALK_LIMITS[1])
        say(f"[{n}/{ITERATIONS}] heading {heading:>3}  walk {walk_s:.1f}s  {reason}"
            + (f"  [FALLBACK: {fallback}]" if fallback else ""))

        turned = await self.nav.turn_to(heading)
        await self.nav.drive_for(WALK_SPEED, 0, walk_s)
        self.visited.append(heading)
        with self.log_path.open("a") as f:
            f.write(json.dumps({
                "t": stamp(), "iteration": n, "model": MODEL,
                "frames": [{"heading": s["heading"], "t": s["t"],
                            "actual_heading": s["actual_heading"], "had_image": bool(s["jpeg"])}
                           for s in shots],
                "raw": raw, "stop_reason": stop, "fallback": fallback,
                "executed": {"heading": heading, "walk_s": round(walk_s, 2),
                             "turned_deg": round(turned, 1),
                             "turn_mode": "imu" if self.nav.imu_ok() else "timed"},
            }) + "\n")

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
            say(await self.nav.start(video=True) + f", log {self.log_path}")
            if REQUIRE_CAMERA and not self.nav.camera_ok():
                say("ABORT  camera is not delivering frames - refusing to run blind "
                    "(power-cycle the dog; set REQUIRE_CAMERA = False to override)")
                return 1
            print(">>> EXPLORING - ESC or Ctrl+C to stop <<<")
            for n in range(1, ITERATIONS + 1):
                await self.iteration(n)
            say(f"Done - {ITERATIONS} iterations")
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
    e = Explore()
    loop = asyncio.get_running_loop()
    install_quit_handlers(loop, e.quit)
    loop.add_reader(sys.stdin.fileno(), e.on_stdin)
    try:
        return await e.run()
    finally:
        loop.remove_reader(sys.stdin.fileno())
        await e.robot.shutdown()


if __name__ == "__main__":
    run_app(amain)
