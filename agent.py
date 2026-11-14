#!/usr/bin/env python3
"""Goal-driven agent: claude-haiku-5-5 decides every action through tools.

  ./run.sh agent "walk to the end of the hallway and come back"

Manual tool-use loop. Tools:
  look(turn_first?)           optional turn, then camera frame (JPEG) + heading
  move(direction, seconds)    forward / left / right, 0.5-3 s
  turn(degrees)               +right / -left, clamped to +/-180, IMU closed-loop
  pose()                      x, y, heading, distance moved since the last pose()
  trick(name)                 hello / dance / stretch / sit / stand
  finish(reason)              ends the run

Startup: API preflight (before he stands), obstacle avoidance ON (won't start
otherwise), BalanceStand. Any API error is loud and fatal: StopMove and exit.
At most MAX_STEPS model calls. ESC or Ctrl+C stops him at any point.
Every step -> runs/<ts>-agent.jsonl. Backward isn't offered: he can't see
behind him.
"""
import argparse
import asyncio
import json
import math
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
EFFORT = "low"
MAX_STEPS = 150            # model calls per run
KEEP_IMAGES = 3            # camera frames kept in history; older -> text placeholder
SPEED = 0.3                # move speed (stick deflection on the avoidance path)
MOVE_LIMITS = (0.5, 3.0)   # seconds
TURN_LIMIT = 180           # degrees
SETTLE_S = 0.4             # pause before look() grabs a frame
REQUIRE_CAMERA = True      # refuse to run if no video frames after warm-up
# ─────────────────────────────────────────────────────────────────────

SYSTEM = """You control a Unitree Go2 robot dog through tools, in a real indoor space with people nearby. Pursue the user's goal one action at a time.

How the body works:
- Obstacle avoidance is active. If you command move("forward") and the dog does not actually move, something is blocking it: call look(), then turn and pick another direction to go around. Never keep pushing into a blocked direction.
- Use pose() after moves to check progress. It reports how far the dog moved since the previous pose() call; a distance near zero after a move means you were blocked.
- The camera faces forward only. The dog cannot see behind itself, so there is no backward move; turn first, then look, then move.
- turn(degrees): positive turns right (clockwise), negative turns left. Headings are degrees clockwise from where the dog faced at startup.
- Keep moves short (1-3 s) and look() often, especially before moving into space you have not seen.

Keep moving. Your default is to walk, not to stop:
- At startup you may be facing a wall or standing in a corner. Turning and walking to a better vantage point is expected and cheap - do it whenever your current view isn't useful.
- Not seeing the target from where you stand is never a reason to finish. Walk toward visible open floor, then look again from the new spot. Repeat as needed.
- If a move produces almost no distance in pose(), you are boxed in on that side - not done. look(), pick a different direction that shows open floor, turn to it, and go there.
- When in doubt between finishing and walking to open floor you can see, walk.

finish(reason) is only for:
1. The goal is achieved.
2. You are genuinely stuck after actually trying: you have looked and attempted several different open directions and none of them got you anywhere.
3. You are out of steps (each tool-result message tells you how many model turns remain).
Say which of these applies in the reason."""

TOOLS = [
    {"name": "look", "description": "Capture one image from the dog's forward camera. Returns the image and the current heading. Optionally rotate first with turn_first (degrees, positive = right, negative = left, max 180) - use this to scan in one step instead of calling turn() then look().",
     "input_schema": {"type": "object", "properties": {
         "turn_first": {"type": "number", "description": "Optional rotation before the photo, -180 to 180"}}}},
    {"name": "move", "description": "Walk for a number of seconds, then stop. forward = straight ahead, left/right = sidestep.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["forward", "left", "right"]},
         "seconds": {"type": "number", "description": "0.5 to 3"}},
         "required": ["direction", "seconds"]}},
    {"name": "turn", "description": "Rotate in place. Positive degrees = right (clockwise), negative = left. Max 180 either way.",
     "input_schema": {"type": "object", "properties": {"degrees": {"type": "number"}},
                      "required": ["degrees"]}},
    {"name": "pose", "description": "Current position (x, y metres, odometry), heading, distance moved since the previous pose() call, and distance from the start.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "trick", "description": "Perform a built-in move: hello (wave), dance, stretch, sit, stand (stand back up after sit).",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "enum": ["hello", "dance", "stretch", "sit", "stand"]}},
         "required": ["name"]}},
    {"name": "finish", "description": "End the run. Say whether the goal was achieved and why.",
     "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                      "required": ["reason"]}},
]
TRICKS = {"hello": [("Hello", None)], "dance": [("Dance1", None)], "stretch": [("Stretch", None)],
          "sit": [("Sit", None)], "stand": [("RiseSit", None), ("BalanceStand", None)]}


def stamp():
    return datetime.now().isoformat(timespec="milliseconds")


def say(line):
    t = time.time()
    s = time.strftime("%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"
    sys.stdout.write(f"\r\033[K{s}  {line}\n")
    sys.stdout.flush()


class Agent:
    def __init__(self, goal):
        self.goal = goal
        self.quit = asyncio.Event()
        self.robot = Robot(self.quit)
        self.nav = Nav(self.robot, self.quit)
        self.client = make_client()
        self.start_pos = None
        self.last_pose_pos = None
        self.finished = None
        self.images = []          # (image block dict inside messages, heading) oldest first
        Path("runs").mkdir(exist_ok=True)
        self.log_path = Path("runs") / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-agent.jsonl")

    def on_stdin(self):
        if any(k in ("ESC", "CTRL_C", "EOF") for k in read_keys()):
            self.quit.set()

    def log(self, **row):
        with self.log_path.open("a") as f:
            f.write(json.dumps({"t": stamp(), **row}, default=str) + "\n")

    # ── tools ───────────────────────────────────────────────────────
    async def tool(self, name, args):
        """Execute one tool. Returns (content for tool_result, short summary)."""
        if name == "look":
            pre = ""
            if args.get("turn_first") not in (None, 0):
                deg = min(max(float(args["turn_first"]), -TURN_LIMIT), TURN_LIMIT)
                turned = await self.nav.turn_by(deg)
                pre = f"Turned {turned:.0f} deg first. "
            await self.nav.pause(SETTLE_S)
            jpeg = self.nav.jpeg_b64()
            heading = round(self.nav.heading())
            h = f"heading {heading} deg"
            if jpeg is None:
                return f"{pre}No camera frame available. {h}.", f"{pre}no frame, {h}"
            image = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": jpeg}}
            self.images.append((image, heading))
            return [image, {"type": "text", "text": f"{pre}Forward camera view, {h}."}], f"{pre}frame, {h}"
        if name == "move":
            direction = args.get("direction")
            secs = min(max(float(args.get("seconds", 1)), MOVE_LIMITS[0]), MOVE_LIMITS[1])
            vec = {"forward": (SPEED, 0), "left": (0, SPEED), "right": (0, -SPEED)}.get(direction)
            if vec is None:
                return f"Unknown direction {direction!r}; use forward, left or right.", "bad direction"
            end = time.monotonic() + secs
            while time.monotonic() < end:
                self.robot.drive(vec[0], vec[1], 0)
                await self.nav.pause(1.0 / self.robot.rate_hz())
            self.robot.drive(0, 0, 0)
            return f"Commanded {direction} for {secs:.1f} s. Call pose() to see how far you actually moved.", f"{direction} {secs:.1f}s"
        if name == "turn":
            deg = min(max(float(args.get("degrees", 0)), -TURN_LIMIT), TURN_LIMIT)
            turned = await self.nav.turn_by(deg)
            return (f"Turned {turned:.0f} deg (asked {deg:.0f}). Heading now {self.nav.heading():.0f} deg.",
                    f"{turned:+.0f} deg -> heading {self.nav.heading():.0f}")
        if name == "pose":
            p = self.nav.position()
            if p is None:
                return "Position unavailable (no odometry).", "no odometry"
            moved = math.hypot(p[0] - self.last_pose_pos[0], p[1] - self.last_pose_pos[1])
            from_start = math.hypot(p[0] - self.start_pos[0], p[1] - self.start_pos[1])
            self.last_pose_pos = p
            out = {"x_m": round(p[0], 2), "y_m": round(p[1], 2), "heading_deg": round(self.nav.heading()),
                   "moved_since_last_pose_m": round(moved, 2), "distance_from_start_m": round(from_start, 2)}
            return json.dumps(out), f"moved {moved:.2f} m, from start {from_start:.2f} m"
        if name == "trick":
            steps = TRICKS.get(args.get("name"))
            if steps is None:
                return f"Unknown trick {args.get('name')!r}.", "bad trick"
            codes = []
            for cmd, param in steps:
                try:
                    codes.append(f"{cmd}={await self.robot.sport(cmd, param)}")
                except asyncio.TimeoutError:
                    codes.append(f"{cmd}=no-reply")
            return f"Robot reply codes (0 = accepted): {', '.join(codes)}", ", ".join(codes)
        if name == "finish":
            self.finished = str(args.get("reason", ""))
            return "Run ended.", self.finished
        return f"Unknown tool {name!r}.", "unknown tool"

    def prune_images(self):
        """Keep only the last KEEP_IMAGES frames in history. Older image blocks
        are rewritten in place to a short text placeholder, so cost and latency
        stay flat instead of growing with every look(). Editing history like
        this invalidates later thinking blocks; the request sends
        prefix_mismatch_behavior="drop_block" so the API drops those blocks
        instead of returning a 400. Returns how many were pruned."""
        old, self.images = self.images[:-KEEP_IMAGES], self.images[-KEEP_IMAGES:]
        for block, heading in old:
            block.clear()
            block.update({"type": "text", "text": f"[earlier camera frame, heading {heading}]"})
        return len(old)

    # ── loop ────────────────────────────────────────────────────────
    async def loop(self):
        messages = [{"role": "user", "content": f"Goal: {self.goal}"}]
        for step in range(1, MAX_STEPS + 1):
            try:
                pruned = self.prune_images()
                response = await self.robot.until_quit(self.client.beta.messages.create(
                    model=MODEL, max_tokens=4096, output_config={"effort": EFFORT},
                    thinking={"type": "adaptive",
                              "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
                    betas=["thinking-binding-controls-2026-08-01"],
                    system=SYSTEM, tools=TOOLS, messages=messages), timeout=None)
            except anthropic.APIError as e:
                raise ApiFatal(describe(e)) from e
            messages.append({"role": "assistant", "content": response.content})
            text = " ".join(b.text for b in response.content if b.type == "text").strip()
            calls = [b for b in response.content if b.type == "tool_use"]
            if text:
                say(f"#{step:<2} says: {text[:200]}")
            row = {"step": step, "stop_reason": response.stop_reason, "text": text,
                   "usage": {"in": response.usage.input_tokens, "out": response.usage.output_tokens},
                   "images_in_history": len(self.images), "pruned_now": pruned,
                   "thinking_dropped": sum(1 for x in (getattr(response, "input_transformations", None) or [])
                                           if getattr(x, "type", None) == "thinking_dropped"),
                   "calls": [], "pose": self.nav.position(), "heading_deg": round(self.nav.heading(), 1)}

            if response.stop_reason == "refusal":
                self.log(**row)
                raise ApiFatal(f"model refused (stop_details: {getattr(response, 'stop_details', None)})")
            if not calls:
                self.log(**row)
                say(f"#{step:<2} model stopped without finish() (stop_reason {response.stop_reason})")
                return f"model stopped: {response.stop_reason}"

            results = []
            for c in calls:
                content, summary = await self.tool(c.name, c.input)
                say(f"#{step:<2} {c.name}({json.dumps(c.input)[:80]}) -> {summary}")
                results.append({"type": "tool_result", "tool_use_id": c.id, "content": content})
                row["calls"].append({"tool": c.name, "input": c.input, "result": summary,
                                     "image": isinstance(content, list)})
            row["pose_after"] = self.nav.position()
            self.log(**row)
            remaining = MAX_STEPS - step
            results.append({"type": "text", "text": f"Model turns remaining: {remaining}."
                            + (" This is your last one - call finish() with an honest reason." if remaining == 1 else "")})
            messages.append({"role": "user", "content": results})
            if self.finished is not None:
                return f"finish: {self.finished}"
        return f"step limit ({MAX_STEPS})"

    async def run(self):
        say(f"GOAL: {self.goal}")
        try:
            say(f"API preflight ({MODEL}) ...")
            await self.robot.until_quit(preflight(self.client, MODEL), timeout=None)
            say("API OK")
            await self.robot.connect()
        except QuitRequested:
            print("Aborted before connecting.")
            return 1
        except ApiFatal as e:
            print(loud(str(e)))
            return 2
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
            self.start_pos = self.last_pose_pos = self.nav.position() or (0.0, 0.0)
            self.log(event="start", goal=self.goal, model=MODEL, start_pos=self.start_pos)
            print(">>> AGENT RUNNING - ESC or Ctrl+C to stop <<<")
            why = await self.loop()
            say(f"END: {why}")
            self.log(event="end", why=why)
        except QuitRequested:
            say("Stopped by ESC / Ctrl+C")
            self.log(event="end", why="ESC / Ctrl+C")
        except ApiFatal as e:
            print(loud(str(e)))
            self.log(event="end", why=f"API FAILURE: {e}")
            return 2
        except asyncio.TimeoutError:
            say("ABORT  robot stopped answering requests")
            return 1
        return 0


async def amain():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--mode")
    p.add_argument("--ip")        # consumed by robot.conn_target()
    p.add_argument("goal", nargs="*")
    args, _ = p.parse_known_args()
    goal = " ".join(args.goal).strip()
    if not goal:
        print('usage: ./run.sh agent "your goal"   (e.g. "find the door and stop in front of it")')
        return 2
    try:
        a = Agent(goal)
    except ApiFatal as e:
        print(loud(str(e)))
        return 2
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
