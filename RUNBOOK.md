# Go2 keyboard driving: runbook

## Before you start
- Clear the floor, **especially behind him**. He doesn't see what's behind him (NOTES.md #1), so S drives backward blind.
- Keep the physical remote in your hand. It's your real emergency stop.
- Close the Unitree app on **every** phone. The robot accepts only one connection at a time.

## Connection mode
`.env` sets the default: `UNITREE_MODE=sta` with `UNITREE_IP=172.20.10.11` (the dog and the laptop on the same network, here the iPhone hotspot). Override for one run with `./run.sh drive --mode ap` (dog's own hotspot) or `--ip <addr>`. Each script prints `Mode:` and `Target:` at startup. On the iPhone hotspot multicast discovery doesn't work. If the dog's IP changes, ask Claude to scan `172.20.10.1-14` for port 9991.

## Run it
1. Power on the dog and wait until he's fully booted.
2. Join this laptop to the **GO2-xxxx** Wi-Fi. The password is `GO2_HOTSPOT_PASSWORD` in `.env`. You'll lose internet; that's expected.
3. In a terminal:
   ```bash
   cd ~/robodog
   ./run.sh
   ```
4. Wait for this line:
   ```
   >>> READY - W/S A/D Q/E to drive, SPACE stop, ESC quit ...
   ```
   Keys pressed before READY are ignored, except ESC and Ctrl+C, which abort.
5. Drive:

   | Key | Action |
   |---|---|
   | W / S | forward / back (x, ±0.3 m/s) |
   | A / D | left / right (y, ±0.3 m/s) |
   | Q / E | turn left / right (yaw, ±0.6 rad/s) |
   | U | MoonWalk (api 1305) |
   | I | Pose on (api 1028, `{"data": true}`); press I again to leave it (StopMove) |
   | O | Dance1 (api 1022) |
   | B | obstacle avoidance on/off (starts **ON**; the status line shows `[AVOID ON]`, `[AVOID OFF]` or `[AVOID ?]`) |
   | + / − | drive speed up / down by 0.1 (min 0.1, max 1.0). Applies right away, even to a key you're holding. Shown as `spd` on the status line. |
   | R | TrotRun on/off (api 1062, newer MCF firmware only). Shown as `RUN on/off`. |
   | L | cycle speed level −1 → 0 → 1 (api 1015). Shown as `lvl`. |
   | SPACE | stop all (also leaves Pose) |
   | ESC or Ctrl+C | StopMove, disconnect, quit |

   While a trick or Pose is running, the stream of Move commands pauses so it doesn't cut the trick short. The status line shows `TRICK` or `POSE`. Any drive key or SPACE starts driving again. Drive keys are ignored while in Pose. A trick prints `-> code N` when the dog replies. A non-zero code means the dog rejected it, most likely because this firmware or mode doesn't support it.

   **Hold** a key to keep moving. An axis drops back to 0 about 0.6 s after you let go, which works as a deadman switch. A terminal only repeats the last key you hold, so W+A held together won't combine reliably.
6. Press ESC. Rejoin your normal Wi-Fi.

Speeds and the hold timeout are at the top of `drive.py` (`SPEED`, `YAW`, `HOLD_TIMEOUT_S`). Set `HOLD_TIMEOUT_S = None` to make keys latch until you press SPACE.

## Obstacle avoidance: how drive commands are routed
- **Avoidance ON:** drive commands go out as a simulated joystick on `rt/wirelesscontroller`. The library's obstacle-avoidance example says the avoidance service filters that path. On this path the numbers are **stick deflection (0.3 = 30% stick), not m/s**, so the real speed depends on the dog's speed level. It's published at 50 Hz, matching the example.
- **Avoidance OFF or unknown:** commands go out as sport `Move` (api 1008, m/s, 10 Hz). Nothing in the library says avoidance filters this path, so treat it as unprotected.
- **Alternative:** `AVOID_DRIVE_PATH = "api"` in `robot.py` switches to the avoidance service's own Move (api 1003, m/s), with a hand-over (api 1004) at start and exit. It's documented in the library's constants, but no library example uses it. Test it with the dog lifted or in open space before relying on it.

## Wander (autonomous)
```bash
./run.sh wander
```
Turns avoidance ON. **It refuses to start if it can't confirm avoidance is on.** Then BalanceStand, and he walks forward at `FORWARD`. After 4–8 s, or when he's moved less than 10 cm in 2 s (stalled, probably because avoidance is holding him), he turns 45–180° in a random direction and carries on. Every decision prints one line. ESC or Ctrl+C stops him. He only walks forward and turns in place, so the blind spot behind him doesn't come into play. Settings are at the top of `wander.py`.

`NO-ODOM` means no position updates are arriving, so stall detection is off and only the timer triggers turns. Avoidance still applies.

## Goal agent (tool use)
```bash
./run.sh agent "walk to the end of the hallway and come back"
```
`claude-haiku-5-5` decides every action through the tools look(turn_first?), move(forward/left/right, 0.5–3 s), turn(±180°), pose(), trick(hello/dance/stretch/sit/stand) and finish(reason). look(turn_first=N) turns, then takes the photo, in one step. pose() reports the distance moved since the previous pose(). The system prompt tells the model that avoidance is on, so a move that goes nowhere means it's blocked and should look and go around. There's no backward move. The run stops after 150 model calls (each tool result tells the model how many remain), on finish() or on ESC. Only the last 3 camera frames stay in the conversation; older ones become `[earlier camera frame, heading N]`, so per-call cost stays roughly flat. That edits history, so requests send `drop_block` (beta `thinking-binding-controls-2026-08-01`): the API drops the model's older *thinking* blocks instead of returning a 400. Its visible text and tool calls are kept. Log: `runs/<ts>-agent.jsonl`.

## API failures are fatal
Every LLM script (agent, live, explore, hallway) makes one tiny API call **before the dog stands**. Any API error then or later prints a `!! API FAILURE - STOPPING THE DOG` banner, sends StopMove and exits with code 2. Nothing falls back to moving. Bad JSON from explore/hallway still falls back as before, but only when the API itself worked. All of them share `llm.py`, which forces **IPv4**: on the iPhone hotspot IPv6 to the API hangs, and each call used to take 30–40 s. The earlier "APIConnectionError on every call" came from an old Brotli library in the venv (fixed by upgrading to brotli ≥ 1.2.0).

## One-action-per-call agent (older proof of concept)
```bash
./run.sh live             # text only
./run.sh live --vision    # one camera frame (JPEG, 640 px wide) per decision
```
Needs `ANTHROPIC_API_KEY=...` in `.env` and internet, so use sta mode, not the dog's hotspot. Model: `MODEL` at the top of `agent_live.py`. Turns avoidance ON (refuses to start otherwise), then BalanceStand. Then it repeats: one API call, one JSON action, execute it, print one line. `duration_s` is clamped to 1–4 s. Moves are re-sent at 10 Hz for that long. Tricks run once, then he holds still for the duration; `pose` is ended with StopMove afterwards. Bad JSON or an unknown action prints and is skipped. Raw replies go to `runs/<timestamp>.jsonl`. ESC or Ctrl+C sends StopMove and quits, even mid-API-call. `backward` is one of the allowed actions, and **he can't see behind him**.

## Explore (look, pick, walk ×3)
```bash
./run.sh explore
```
Needs `ANTHROPIC_API_KEY` and internet (sta mode). Avoidance ON, otherwise it won't start. Each round:
1. Turns to face heading 0, then right in 90° steps, grabbing a frame at 0/90/180/270.
2. Sends all four labelled frames to `claude-haiku-5-5`, along with the headings already walked toward.
3. Turns to the chosen heading and walks 2–5 s.

Headings are degrees clockwise from the direction he faced at startup. Turns stop on the IMU yaw. Without IMU data they fall back to timed turns, which is uncalibrated, and the startup line says `IMU MISSING`. Bad JSON, a refusal or an API error picks a random heading, logged as `FALLBACK`. Log: `runs/<ts>-explore.jsonl`.

## Hallway (out to the end and back)
```bash
./run.sh hallway
```
Needs `ANTHROPIC_API_KEY` and internet (sta mode). Avoidance ON, otherwise it won't start. **Start him facing down the hallway.** His starting IMU yaw becomes the hallway heading, and his starting `rt/utlidar/robot_pose` becomes home.
- **Out:** each burst sends one camera frame to `claude-haiku-5-5`, which answers `forward`, `adjust_left`/`adjust_right` (a ~15° turn, then walk) or `at_end`. After each burst he turns back if he's more than 10° off the hallway heading. The leg ends on `at_end` or after 15 bursts.
- **Turn:** 180° on the IMU.
- **Back:** the same, and he also stops within 0.5 m of home.

walk_s is clamped to 1–3 s. Bad JSON means forward 1.5 s, logged `FALLBACK`. Log: `runs/<ts>-hallway.jsonl`.

## Sensor probe (read-only, no motion)
```bash
./run.sh probe
```
Listens for 10 s, then reports message counts for the lidar point cloud, lidar state, lidar pose, sport state and camera frames. `YES` means the data arrived over this connection.

## What the messages mean

| Message | Meaning | Fix |
|---|---|---|
| `ERROR SIGNALING_UNREACHABLE` | Can't reach 192.168.12.1:9991 | You're not on the GO2 Wi-Fi, or the dog is still booting. Check the Wi-Fi and wait 30 s. |
| `ERROR TIMEOUT` | No connection within 30 s | Same as above. If it keeps happening, power-cycle the dog. |
| `ERROR ROBOT_BUSY` | Something else holds his one connection | Close the app on every phone. A drive.py that died uncleanly can also hold it for a minute or so; wait, or power-cycle him. |
| `ERROR AES_KEY_REQUIRED` | The firmware now wants the per-device key (Go2 firmware ≥ 1.1.15). This didn't happen on your last connect. | Put `UNITREE_AES_128_KEY=...` in `.env`. The cloud fetch (`get_key.py`) was blocked by Unitree's bot check last time, so retry it while you have internet. |
| `ERROR AES_KEY_REJECTED` | The key in `.env` is wrong for this robot | Re-fetch it, or delete the line if he didn't need one before. |
| `Motion mode: (no answer ...)` | Normal on newer firmware | Ignore it. |
| `BalanceStand -> code N` (non-zero) or `no reply` | He didn't take the stand command | Stand him up with the remote, then run again. |
| READY shows but he doesn't move | Commands are arriving but being ignored | Make sure he's standing (not lying or damped) and that the remote isn't in a mode that overrides app control. Try ESC, then `./run.sh` again. |
| `Avoidance ON -> code N ... did not take` / `[AVOID ?]` | The avoidance service rejected the switch or didn't answer. The Air may not support it. | In drive.py you're on the unprotected sport-Move path; drive carefully. Wander won't start. |
| `ABORT obstacle avoidance not confirmed ON` (wander) | Same as above | Don't wander. Run `./run.sh probe` and check the code you got. |
| Avoidance ON but he won't move with W | The avoidance service may need joystick input to be allowed from the API | Press B to turn avoidance off and drive on sport Move, or try `AVOID_DRIVE_PATH = "api"`. Tell Claude the codes you saw. |
| `TrotRun ... -> code N` (non-zero) | This firmware doesn't support TrotRun. It's only in the newer MCF command table. | Use + and L for speed instead. |
| `SpeedLevel ... -> code N` (non-zero), or `lvl ?` | Level change rejected, or the robot didn't report its starting level | `lvl ?` only means the start level is unknown. The first L press sets 0 (normal). |
| `429 Too Many Requests` / `NoSdpAnswerError` | You reconnected too soon after the last session | Wait about 20 s and run it again. |
| `DataChannelTimeoutError` (stuck at ICE `checking`) | Another client holds the dog's connection, or Windows is blocking incoming UDP | Force-quit the Unitree app, and set the Wi-Fi network to **Private** in Windows. |
| `StopMove failed` on exit | The connection dropped before the stop got through | **Use the remote to stop him.** |

## If he won't stop
Use the physical remote. Killing the laptop side stops new Move commands, but don't count on that alone.

## If the terminal gets stuck
`drive.py` restores the terminal on ESC, Ctrl+C and errors, and `run.sh` runs `stty sane` on exit as a backup. If the terminal is still broken (no echo, Enter does nothing, garbled output):
1. Press Ctrl+J, then type `reset` and press Ctrl+J again. You won't see what you type. Ctrl+J works as Enter when Enter is broken.
2. If that doesn't work, type `stty sane` the same way.
3. If drive.py is still running and keys do nothing, open another terminal and run `pkill -f drive.py`. It shuts down cleanly on that signal and sends StopMove.
4. As a last resort, close the terminal window. **Stop the dog with the remote first.**
