#!/usr/bin/env bash
# Keeps the dashboard running at http://localhost:8050 (starts with your Mac, restarts if it stops).
# Read-only: it never places orders. Turn it off with ops/uninstall-dashboard.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
LABEL="com.personaltrader.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__ROOT__#$ROOT#g" -e "s#__UV__#$UV#g" "$ROOT/ops/launchd/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Dashboard is running at http://localhost:8050 (log: $ROOT/data/logs/dashboard.log)"
