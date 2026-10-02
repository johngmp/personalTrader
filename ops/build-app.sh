#!/usr/bin/env bash
# Builds "Personal Trader.app" — a real Mac app bundle wrapping the local dashboard.
#
# It is a thin launcher: the window is a native WKWebView showing http://localhost:8050, which is the
# same page `trader serve` renders. No second copy of the UI exists. Double-clicking starts the server
# if nothing is listening, so it works from cold.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV="$(command -v uv)"
APP="${1:-$HOME/Applications/Personal Trader.app}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Personal Trader</string>
  <key>CFBundleDisplayName</key><string>Personal Trader</string>
  <key>CFBundleIdentifier</key><string>com.personaltrader.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>PersonalTrader</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/PersonalTrader" <<LAUNCH
#!/bin/bash
# Launched by Finder, which gives a minimal PATH, so uv is called by its full path.
cd "$ROOT" || exit 1
exec "$UV" run --project "$ROOT" trader app
LAUNCH
chmod +x "$APP/Contents/MacOS/PersonalTrader"

# Icon: a simple generated mark, so the Dock does not show a blank page.
ICONSET="$(mktemp -d)/icon.iconset"; mkdir -p "$ICONSET"
cat > "$ICONSET/icon.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024">
  <rect width="1024" height="1024" rx="230" fill="#141412"/>
  <polyline points="190,700 370,540 530,620 840,300" fill="none" stroke="#c98500"
            stroke-width="76" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="840" cy="300" r="54" fill="#c98500"/>
</svg>
SVG
if command -v qlmanage >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  qlmanage -t -s 1024 -o "$ICONSET" "$ICONSET/icon.svg" >/dev/null 2>&1 || true
  SRC="$ICONSET/icon.svg.png"
  if [ -f "$SRC" ]; then
    for s in 16 32 64 128 256 512; do
      sips -z $s $s "$SRC" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
      sips -z $((s*2)) $((s*2)) "$SRC" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
    done
    rm -f "$ICONSET/icon.svg" "$SRC"
    iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns" 2>/dev/null || true
  fi
fi

echo "Built: $APP"
echo "Open it from Launchpad or Spotlight (\"Personal Trader\"), or: open -a 'Personal Trader'"
