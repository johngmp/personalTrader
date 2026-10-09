#!/usr/bin/env bash
# Turns OFF minute-by-minute forex watching. The engine's own fifteen-minute pass still runs the
# watcher, so you keep the daily plan and the slower alerts; you lose the quick ones.
set -euo pipefail
LABEL="com.personaltrader.fxwatch"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Forex watching is OFF. The engine still checks every 15 minutes."
