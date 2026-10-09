#!/usr/bin/env bash
# Turns ON minute-by-minute forex watching: runs `trader watch-fx` every 60 seconds.
#
# This sends messages and places nothing, so it is not gated on paper mode the way the engine is -- it
# cannot reach a broker at all. Turn it off again with ops/uninstall-fxwatch.sh. The kill switch stops
# it too: a halted system that keeps texting you trades to place by hand has not really halted.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
LABEL="com.personaltrader.fxwatch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

grep -q '^enabled: true' "$ROOT/config/intraday.yaml" || {
  echo "Refusing: config/intraday.yaml has enabled: false, so there is nothing to watch." >&2; exit 1; }
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__ROOT__#$ROOT#g" -e "s#__UV__#$UV#g" "$ROOT/ops/launchd/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Forex watching is ON: every 60 seconds between 07:00 and 16:00 UTC on weekdays."
echo "Pairs: $(grep -A5 '^symbols:' "$ROOT/config/intraday.yaml" | grep '^  - ' | tr -d ' -' | paste -sd, -)"
echo "Log:   $ROOT/data/logs/fxwatch.log"
echo
echo "A resting limit order at the level still beats any polling frequency -- it fills AT your price"
echo "and misses nothing. This makes the push message quicker; it does not replace that advice."
