#!/usr/bin/env bash
# Keeps the P&L menu bar item running (starts with your Mac, restarts if it stops).
# Read-only: it never places orders. Turn it off with ops/uninstall-menubar.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
LABEL="com.personaltrader.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__ROOT__#$ROOT#g" -e "s#__UV__#$UV#g" "$ROOT/ops/launchd/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Menu bar item is running — look for \"PT\" in your menu bar (log: $ROOT/data/logs/menubar.log)"
