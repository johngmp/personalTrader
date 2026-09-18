#!/usr/bin/env bash
# Turns ON automatic paper trading: runs `trader run` every 15 minutes in the background.
# Turn it off again with ops/uninstall-engine.sh (or pause trading any time with `uv run trader halt`).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
LABEL="com.personaltrader.engine"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

grep -q '^mode: paper' "$ROOT/config/risk.yaml" || { echo "Refusing: config/risk.yaml is not in paper mode." >&2; exit 1; }
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s#__ROOT__#$ROOT#g" -e "s#__UV__#$UV#g" "$ROOT/ops/launchd/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Automatic paper trading is ON: the engine runs now and then every 15 minutes."
echo "Log:       $ROOT/data/logs/engine.log"
echo "Dashboard: $ROOT/data/dashboard.html"
