#!/usr/bin/env bash
# Turns OFF automatic paper trading. Positions and their stop orders at Alpaca stay as they are.
set -euo pipefail
LABEL="com.personaltrader.engine"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Automatic paper trading is OFF. Existing positions and stops at Alpaca are unchanged."
