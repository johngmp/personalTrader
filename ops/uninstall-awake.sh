#!/usr/bin/env bash
# Lets the Mac sleep normally again.
set -euo pipefail
LABEL="com.personaltrader.awake"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed. The Mac will idle-sleep normally again."
