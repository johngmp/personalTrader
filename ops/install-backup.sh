#!/usr/bin/env bash
# Turns ON nightly backups of the trading journal. Off again with ops/uninstall-backup.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.personaltrader.backup"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/data/logs" "$HOME/Library/LaunchAgents"
UV="$(command -v uv)"
sed -e "s#__ROOT__#$ROOT#g" -e "s#__UV__#$UV#g" "$ROOT/ops/launchd/$LABEL.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Nightly journal backup is ON (03:10 local, and once now)."
echo "Backups: ${PT_BACKUP_DIR:-$HOME/Library/Application Support/PersonalTrader/backups}"
echo "Log:     $ROOT/data/logs/backup.log"
