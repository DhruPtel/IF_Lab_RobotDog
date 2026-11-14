#!/usr/bin/env bash
# Usage (after joining the GO2-xxxx wifi):
#   ./run.sh           keyboard driving   (drive.py)
#   ./run.sh wander    autonomous wander  (wander.py)
#   ./run.sh probe     read-only lidar/camera check (sensors_probe.py)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
source .venv/bin/activate
#   Extra args pass through, e.g.  ./run.sh drive --mode ap
#                                  ./run.sh probe --mode sta --ip 172.20.10.11
cmd=drive
#   ./run.sh agent "goal"   tool-use agent (agent.py)
#   ./run.sh live [--vision]  one-action-per-call agent (agent_live.py)
#   ./run.sh four      4 random moves, smoke test (four_moves.py)
#   ./run.sh explore   scan 4 ways, Claude picks a heading, walk; x3 (explore.py)
#   ./run.sh hallway   walk to the end of the hallway and back (hallway.py)
#   ./run.sh avoidtest  measure avoidance ON vs OFF speed (avoid_test.py)
case "${1:-}" in drive|wander|probe|agent|live|four|explore|hallway|avoidtest) cmd=$1; shift ;; esac
case "$cmd" in
  hallway) script=hallway.py ;;
  avoidtest) script=avoid_test.py ;;
  explore) script=explore.py ;;
  four)   script=four_moves.py ;;
  drive)  script=drive.py ;;
  wander) script=wander.py ;;
  probe)  script=sensors_probe.py ;;
  agent)  script=agent.py ;;
  live)   script=agent_live.py ;;
esac
# If the script is killed hard, put the terminal back to normal anyway.
trap 'stty sane 2>/dev/null || true' EXIT
python "$script" "$@"
