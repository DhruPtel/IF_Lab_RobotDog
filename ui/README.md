# Council feed UI

A feed for the showcase, laid out like the desktop X (Twitter) timeline. The audience sees the agents propose, question, veto and run each move the dog makes, alongside a top-down map of where the dog has walked.

**Status: layout only.** `feed.html` plays back a scripted sample mission (`SAMPLE_EVENTS` in the file). It isn't connected to the dog or to any agent yet.

## Run it

Open `ui/feed.html` in a browser. There's no build step, nothing to install and nothing loaded from the internet.

It uses X's dark theme and column widths (275 / 600 / 350 px), but its own logo, icons and content.

| X element | Here |
|---|---|
| Left nav | Home, Explore (search posts), Notifications (approvals, vetoes, checks), Agents, Map, Log (raw JSON lines), More (playback settings) |
| Account switcher | The dog, `@robodog` |
| Compose box | Type a new mission and press **Post** to restart the replay with it |
| For you / Following | **Live** (every post) / **Decisions** (proposals, vetoes, approvals) |
| Post actions | Replies, approvals (green), vetoes (pink), step number |
| Profiles | One per agent, with Posts / Replies / Actions tabs |
| Right sidebar | Mission, mission steps, map and dog status, the council |
| Floating buttons | Pause/play and playback speed (1×, 2×, 4×) |

Below 1280 px wide the nav collapses to icons, and below 1000 px the right sidebar hides, as on X.

## The agents

| Agent | Handle | Job |
|---|---|---|
| Planner | `@planner` | Breaks the goal into steps and proposes the route |
| Scout | `@scout` | Reads camera frames and reports what's ahead |
| Safety | `@safety` | Checks every proposal for people and obstacles, and can veto it |
| Pilot | `@pilot` | The only agent that calls the dog's tools (`look`, `move`, `turn`, `pose`, `trick`, `finish`) |
| Scribe | `@scribe` | Checks progress against the goal and keeps the record |

The Pilot's tools are the same ones `agent.py` already exposes, so today's single agent maps onto the Pilot.

## Event shape

Each post is one event. The shape is meant to be written as one JSON line per event, like the `runs/*.jsonl` logs `agent.py` already writes:

```json
{"id": 9, "t": "...", "agent": "pilot", "kind": "action",
 "text": "One more.",
 "reply_to": 6,
 "action": {"tool": "move", "args": {"direction": "forward", "seconds": 2},
            "result": "Moved 0.04 m. Blocked.", "ok": false, "frame": null},
 "votes": [["safety", "yes"]],
 "pose": {"x": 1.67, "y": 2.0, "h": 0},
 "step_done": 1, "step_active": 2}
```

| Field | Meaning |
|---|---|
| `agent` | `planner`, `scout`, `safety`, `pilot`, `scribe` or `system` |
| `kind` | `mission`, `proposal`, `check`, `objection`, `approval`, `action`, `finding` or `summary` |
| `reply_to` | `id` of the post this answers. Vetoes and approvals quote it. |
| `action` | The tool call the Pilot made and what came back. `frame` is a caption today and a camera JPEG later. |
| `votes` | How the other agents voted on a proposal |
| `pose` | Dog position in metres from the room's top-left corner, with heading in degrees. It drives the map. |
| `step_done` / `step_active` | Index into the mission's step list on the left |

The room layout (size, chair, people, goal zone) is in `MISSION` near the top of the script.

## Wiring it up later

1. Have each agent write events in the shape above to a shared `runs/<ts>-council.jsonl`.
2. Swap the `SAMPLE_EVENTS` playback for a live source. The simplest option is a small server that tails the JSONL and streams new lines to the page with Server-Sent Events. Each line goes straight into `push(event)`.
3. Replace the `frame` caption with the real JPEG the Pilot's `look()` captured.
4. Build the room layout from the lidar map instead of hand-placing it in `MISSION`.
