#!/usr/bin/env bash
# Stops the Mac idle-sleeping, so the engine keeps its 15-minute schedule.
#
# The engine's gaps are the Mac sleeping: 11 gaps over an hour in 10 days, the worst 7.9 hours. Positions
# stay protected while asleep (stops rest at the broker), but no new decisions are made.
#
# -i prevents idle sleep; -s prevents system sleep while on AC power. The display still sleeps normally.
# This CANNOT override closing the lid: a shut laptop sleeps regardless. Remove with uninstall-awake.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.personaltrader.awake"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__ROOT__#$ROOT#g" "$ROOT/ops/launchd/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Idle sleep is now prevented. Check with: pmset -g | grep ' sleep'"
echo "Note: closing the lid still sleeps the Mac; caffeinate cannot override that."
