#!/usr/bin/env bash
# Turns OFF nightly journal backups. Existing backup files are left alone.
set -euo pipefail
LABEL="com.personaltrader.backup"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Nightly journal backup is OFF. Existing backups kept."
