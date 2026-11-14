#!/usr/bin/env python3
"""Proof of concept: Claude picks the dog's next action, one call at a time.

Loop: one API call -> parse one JSON action -> execute -> log -> repeat.
Bare bones on purpose: no retries, no state machine. Bad JSON is printed and
the step is skipped.

  ./run.sh agent             text only
  ./run.sh agent --vision    attach one camera frame (JPEG, ~640px) per call

ESC or Ctrl+C: StopMove and exit. Raw responses go to runs/<timestamp>.jsonl.
"""
import asyncio
import base64
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic

from llm import ApiFatal, describe, loud, make_client, preflight
from robot import (
    SPORT_CMD,
    ConnectFailed,
    QuitRequested,
    Robot,
    install_quit_handlers,
    read_keys,
    run_app,
)

# ─── Configuration ───────────────────────────────────────────────────
MODEL = "claude-haiku-4-5"
SPEED = 0.3            # forward/backward/left/right (stick deflection with avoidance on)
YAW = 0.6              # turn_left/turn_right
MOVE_HZ = 10           # moves are re-sent at this rate for duration_s
DURATION_LIMITS = (1, 4)   # duration_s is clamped to this (safety, not validation)
HISTORY = 5
FRAME_WIDTH = 640
# ─────────────────────────────────────────────────────────────────────

PROMPT = (
    "You are controlling a robot dog exploring a room. Available actions: "
    "forward, backward, left, right, turn_left, turn_right, dance, pose, "
    "moonwalk, sit, stand, hello, wait. Respond with JSON only: "
    '{"action": "...", "duration_s": 1-4, "reason": "one short sentence"}. '
    "Wander naturally and try to cover different parts of the room."
)
VISION_SUFFIX = "Here is what the dog currently sees."

# action -> (x, y, yaw) velocity, streamed for duration_s
MOVES = {
    "forward": (SPEED, 0, 0), "backward": (-SPEED, 0, 0),
    "left": (0, SPEED, 0), "right": (0, -SPEED, 0),
    "turn_left": (0, 0, YAW), "turn_right": (0, 0, -YAW),
    "wait": (0, 0, 0),
}
# action -> list of (SPORT_CMD name, parameter); then hold still for duration_s
SPORTS = {
    "dance": [("Dance1", None)],
    "pose": [("Pose", {"data": True})],          # left again with StopMove after
    "moonwalk": [("MoonWalk", None)],
    "sit": [("Sit", None)],
    "stand": [("RiseSit", None), ("BalanceStand", None)],
    "hello": [("Hello", None)],
}


def now_str():
    t = time.time()
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


def say(line):
    sys.stdout.write("\r\033[K" + line + "\n")
    sys.stdout.flush()


def parse_action(text):
    """json.loads, tolerating a ```json fence around it. Raises on bad JSON."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return json.loads(s)


class Agent:
    def __init__(self, vision: bool):
        self.vision = vision
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.client = make_client()
        self.history = []
        Path("runs").mkdir(exist_ok=True)
        self.log_path = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    # ── camera ──────────────────────────────────────────────────────
    def jpeg_b64(self):
        img = self.robot.frame.to_image()       # PIL image (PyAV)
        h = round(img.height * FRAME_WIDTH / img.width)
        img = img.resize((FRAME_WIDTH, h))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.standard_b64encode(buf.getvalue()).decode()

    # ── one decision ────────────────────────────────────────────────
    def build_content(self):
        text = PROMPT
        if self.history:
            recent = "\n".join(f"- {h['action']} {h['duration_s']}s: {h['reason']}"
                               for h in self.history[-HISTORY:])
            text += f"\n\nYour last {len(self.history[-HISTORY:])} actions (oldest first):\n{recent}"
        if not self.vision:
            return text
        if self.robot.frame is None:
            return text + "\n\n(No camera frame available yet.)"
        return [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": self.jpeg_b64()}},
            {"type": "text", "text": text + "\n\n" + VISION_SUFFIX},
        ]

    async def decide(self):
        response = await self.robot.until_quit(self.client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": self.build_content()}],
        ), timeout=None)
        raw = next((b.text for b in response.content if b.type == "text"), "")
        with self.log_path.open("a") as f:
            f.write(json.dumps({"t": datetime.now().isoformat(timespec="milliseconds"),
                                "vision": self.vision, "raw": raw}) + "\n")
        return raw

    async def hold(self, x, y, z, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.quit.is_set():
            self.robot.drive(x, y, z)
            try:
                await asyncio.wait_for(self.quit.wait(), 1.0 / MOVE_HZ)
            except asyncio.TimeoutError:
                pass
        self.robot.drive(0, 0, 0)

    async def execute(self, action, duration):
        if action in MOVES:
            await self.hold(*MOVES[action], duration)
            return ""
        codes = []
        for name, param in SPORTS[action]:
            try:
                codes.append(f"{name}={await self.robot.sport(name, param)}")
            except asyncio.TimeoutError:
                codes.append(f"{name}=no-reply")
        try:
            await asyncio.wait_for(self.quit.wait(), duration)
        except asyncio.TimeoutError:
            pass
        if action == "pose":
            codes.append(f"StopMove={await self.robot.sport('StopMove')}")
        return "  [" + " ".join(codes) + "]"

    # ── main ────────────────────────────────────────────────────────
    async def run(self):
        try:
            say(f"{now_str()}  API preflight ({MODEL}) ...")
            await self.robot.until_quit(preflight(self.client, MODEL), timeout=None)
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
                say(f"{now_str()}  ABORT  obstacle avoidance not confirmed ON (code {code})")
                return 1
            say(f"{now_str()}  AVOID-ON  path {self.robot.path_name()}")
            say(f"{now_str()}  STAND  BalanceStand -> code {await self.robot.sport('BalanceStand')}")
            if self.vision:
                n = await self.robot.start_video(8.0)
                say(f"{now_str()}  CAMERA  {f'{n} frames received' if n else 'NO FRAMES after 8 s - the dog is not sending video'}")
            print(f">>> AGENT RUNNING - model {MODEL}, vision {'ON' if self.vision else 'off'}, "
                  f"log {self.log_path} - ESC or Ctrl+C to stop <<<")
            while not self.quit.is_set():
                raw = await self.decide()
                try:
                    d = parse_action(raw)
                    action = str(d["action"])
                    duration = min(max(float(d["duration_s"]), DURATION_LIMITS[0]), DURATION_LIMITS[1])
                    reason = str(d.get("reason", ""))
                except (ValueError, KeyError, TypeError) as e:
                    say(f"{now_str()}  BAD-JSON  {type(e).__name__}: {raw!r}")
                    continue
                if action not in MOVES and action not in SPORTS:
                    say(f"{now_str()}  UNKNOWN  {action!r} - skipped")
                    continue
                say(f"{now_str()}  {action:<10} {duration:.1f}s  {reason}")
                result = await self.execute(action, duration)
                if result:
                    say(f"{'':12}  {'':10}       {result.strip()}")
                self.history.append({"action": action, "duration_s": duration, "reason": reason})
        except QuitRequested:
            pass
        except asyncio.TimeoutError:
            say(f"{now_str()}  ABORT  robot stopped answering requests")
            return 1
        except anthropic.APIError as e:
            print(loud(describe(e)))
            return 2
        say(f"{now_str()}  QUIT  ESC / Ctrl+C")
        return 0


async def amain():
    a = Agent(vision="--vision" in sys.argv)
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
