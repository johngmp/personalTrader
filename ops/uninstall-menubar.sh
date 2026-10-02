#!/usr/bin/env bash
# Stops the always-on menubar. Trading is not affected.
set -euo pipefail
LABEL="com.personaltrader.menubar"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Dashboard stopped. Trading is not affected."
