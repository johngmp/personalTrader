#!/usr/bin/env bash
# Downloads Dukascopy tick history in the background. Safe to run again at any time: it skips every day
# already on disk, so a stop costs nothing and a re-run resumes.
#
# Deliberately nohup rather than a launchd agent: launchd kept returning EX_CONFIG for this one, and a
# resumable download does not need to survive a reboot badly enough to justify more time on that.
#   stop with:   pkill -f download_ticks.py
#   watch with:  tail -f data/logs/dukascopy.log
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/data/logs"
if pgrep -f download_ticks.py > /dev/null; then
  echo "Already running. Days cached: $(ls "$ROOT"/data/ticks/EURUSD_5m 2>/dev/null | wc -l | tr -d ' ')"
  exit 0
fi
cd "$ROOT"
nohup "$ROOT/.venv/bin/python" "$ROOT/ops/download_ticks.py" >> "$ROOT/data/logs/dukascopy.log" 2>&1 &
echo "Started. Days cached so far: $(ls "$ROOT"/data/ticks/EURUSD_5m 2>/dev/null | wc -l | tr -d ' ')"
echo "Watch:  tail -f $ROOT/data/logs/dukascopy.log"
