#!/usr/bin/env bash
# Build a standalone .app and arm64 .dmg for HC3 Menu.
#
# Output: dist/HC3-Menu-<version>-arm64.dmg
#
# Requirements:
#   - macOS arm64 (Apple Silicon)
#   - Python venv with project deps + py2app installed
#   - hdiutil (built-in)
#
# This is an UNSIGNED build. End users must run:
#   xattr -dr com.apple.quarantine /Applications/HC3\ Menu.app
# to bypass Gatekeeper after dragging from the DMG.
set -euo pipefail

cd "$(dirname "$0")/.."

# Detect architecture — arm64 only for v1.
ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
    echo "ERROR: this build script targets arm64 (Apple Silicon) only."
    echo "  Current arch: $ARCH"
    exit 1
fi

# Pick up the venv if present.
if [[ -z "${VIRTUAL_ENV:-}" && -d ".venv" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Read version from the package.
VERSION="$(python -c 'from hc3menu.__version__ import __version__; print(__version__)')"
echo ">>> Building HC3 Menu v${VERSION} (arm64)"

# Ensure py2app is available.
python -c "import py2app" 2>/dev/null || {
    echo ">>> Installing py2app..."
    pip install py2app
}

# Clean previous builds.
rm -rf build dist

echo ">>> Running py2app..."
python setup.py py2app

APP_PATH="dist/HC3 Menu.app"
if [[ ! -d "$APP_PATH" ]]; then
    echo "ERROR: expected $APP_PATH not found"
    exit 1
fi

echo ">>> Stripping extended attributes..."
xattr -cr "$APP_PATH"

echo ">>> Ad-hoc codesigning (no Developer ID)..."
codesign --force --deep --sign - "$APP_PATH"

DMG_NAME="HC3-Menu-${VERSION}-arm64.dmg"
DMG_PATH="dist/${DMG_NAME}"
STAGING="dist/dmg_staging"

echo ">>> Creating DMG: $DMG_PATH"
rm -rf "$STAGING" "$DMG_PATH"
mkdir -p "$STAGING"
cp -R "$APP_PATH" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# Add a small README for unsigned-build instructions.
cat > "$STAGING/README.txt" <<EOF
HC3 Menu v${VERSION}

Install:
  1. Drag "HC3 Menu.app" to the Applications folder.
  2. macOS will refuse to open the unsigned app the first time.
     Open Terminal and run:

         xattr -dr com.apple.quarantine "/Applications/HC3 Menu.app"

  3. Launch HC3 Menu from Applications. Open Preferences and
     fill in your HC3 host, user, password, and (optionally) PIN.

  4. Allow Local Network access (REQUIRED to reach the HC3).
     The first time HC3 Menu tries to connect, macOS should
     prompt: "HC3 Menu would like to find devices on your
     local network." Click Allow.

     If you don't see the prompt and connections fail with
     "No route to host" (errno 65), enable it manually in:

         System Settings -> Privacy & Security -> Local Network
         -> toggle "HC3 Menu" on.

     If "HC3 Menu" is not listed, reset its privacy state and
     relaunch so the prompt re-appears:

         tccutil reset All com.jangabrielsson.hc3menu
         open "/Applications/HC3 Menu.app"

     After allowing Local Network access, QUIT HC3 Menu
     (Cmd-Q from its menu) and launch it again. macOS only
     applies the new permission to a fresh process.

This is a personal-use, unsigned build. Use at your own risk.
EOF

hdiutil create \
    -volname "HC3 Menu ${VERSION}" \
    -srcfolder "$STAGING" \
    -ov -format UDZO \
    "$DMG_PATH"

rm -rf "$STAGING"

echo ""
echo "==============================================="
echo "  Built: $DMG_PATH"
SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo "  Size:  $SIZE"
echo "==============================================="
