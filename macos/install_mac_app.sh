#!/bin/bash
# Build a thin TimeTrackr.app that launches the repo via uv, install it into
# the Applications folder, and (optionally) register it as a login item.
#
#   ./macos/install_mac_app.sh            # build + install + add login item
#   ./macos/install_mac_app.sh --no-login # build + install, skip login item
#
# The bundle is a thin wrapper: it stores absolute paths to this repo and to
# uv, and runs `uv run main.py`. It therefore depends on the repo staying put;
# re-run this script after moving the repo.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
APP_NAME="TimeTrackr"
BUNDLE_ID="com.offworldlabs.timetrackr"
ADD_LOGIN_ITEM=1
[ "${1:-}" = "--no-login" ] && ADD_LOGIN_ITEM=0

# --- locate uv (Finder-launched apps have a minimal PATH, so bake an abs path)
UV_BIN="$(command -v uv || true)"
for cand in "$HOME/.local/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [ -z "$UV_BIN" ] && [ -x "$cand" ] && UV_BIN="$cand"
done
[ -z "$UV_BIN" ] && { echo "error: uv not found on PATH or common locations"; exit 1; }
echo "uv:   $UV_BIN"
echo "repo: $REPO_DIR"

# --- pick a python to render the icon (repo .venv if present, else uv's)
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    ICON_PY="$REPO_DIR/.venv/bin/python"
else
    ICON_PY="$UV_BIN run --project \"$REPO_DIR\" python"
fi

# --- assemble the bundle in a staging dir, then move it into place -----------
BUILD_DIR="$REPO_DIR/macos/build"
APP="$BUILD_DIR/$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Info.plist — LSUIElement=1 makes it a menu-bar (accessory) app: no Dock icon.
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$APP_NAME</string>
    <key>CFBundleDisplayName</key><string>$APP_NAME</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key><string>0.1.0</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleExecutable</key><string>$APP_NAME</string>
    <key>CFBundleIconFile</key><string>$APP_NAME</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Launcher — cd into the repo and run via uv, logging for easy debugging.
cat > "$APP/Contents/MacOS/$APP_NAME" <<LAUNCH
#!/bin/bash
cd "$REPO_DIR" || exit 1
exec "$UV_BIN" run main.py >> "\$HOME/Library/Logs/$APP_NAME.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/$APP_NAME"

# Icon
eval "$ICON_PY \"$REPO_DIR/macos/appicon.py\" \"$APP/Contents/Resources/$APP_NAME.icns\""

# --- install into Applications ----------------------------------------------
if [ -w /Applications ]; then
    DEST_DIR=/Applications
else
    DEST_DIR="$HOME/Applications"
    mkdir -p "$DEST_DIR"
fi
DEST="$DEST_DIR/$APP_NAME.app"
rm -rf "$DEST"
cp -R "$APP" "$DEST"
# Refresh Launch Services / icon caches so it appears promptly.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$DEST" 2>/dev/null || true
touch "$DEST"
echo "installed: $DEST"

# --- login item --------------------------------------------------------------
if [ "$ADD_LOGIN_ITEM" -eq 1 ]; then
    osascript -e "tell application \"System Events\" to delete (every login item whose name is \"$APP_NAME\")" 2>/dev/null || true
    osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$DEST\", hidden:false}" \
        && echo "login item added (starts at login)" \
        || echo "note: could not add login item automatically — add $DEST under System Settings > General > Login Items"
fi

echo "Done. Launch '$APP_NAME' from Launchpad/Spotlight, or it will start at next login."
