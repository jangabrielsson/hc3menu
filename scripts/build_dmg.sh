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
# Signing & notarization:
#   By default the script signs with the developer identity below and
#   submits the DMG to Apple for notarization. Override via env vars:
#
#     SIGN_IDENTITY     full identity string, or empty/"-" for ad-hoc
#     NOTARY_PROFILE    keychain profile name from `notarytool store-credentials`
#                       set to empty string to skip notarization.
#
# Examples:
#   ./scripts/build_dmg.sh                    # signed + notarized + stapled
#   SIGN_IDENTITY=- NOTARY_PROFILE= ./scripts/build_dmg.sh   # ad-hoc, no notarize
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

SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: Jan Gabrielsson (TCU23SBY78)}"
NOTARY_PROFILE="${NOTARY_PROFILE-hc3menu-notary}"
ENTITLEMENTS="scripts/entitlements.plist"

if [[ "$SIGN_IDENTITY" == "-" || -z "$SIGN_IDENTITY" ]]; then
    echo ">>> Ad-hoc codesigning (no Developer ID)..."
    codesign --force --deep --sign - "$APP_PATH"
else
    echo ">>> Codesigning with: $SIGN_IDENTITY"
    # Sign nested binaries first (deepest first) so the outer signature is valid.
    # --deep is deprecated for production but acceptable for a py2app bundle here;
    # we additionally sign the .app explicitly with hardened runtime + entitlements.
    codesign --force --deep --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" \
        --sign "$SIGN_IDENTITY" \
        "$APP_PATH"
    echo ">>> Verifying signature..."
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
fi

DMG_NAME="HC3-Menu-${VERSION}-arm64.dmg"
DMG_PATH="dist/${DMG_NAME}"
STAGING="dist/dmg_staging"

echo ">>> Creating DMG: $DMG_PATH"
rm -rf "$STAGING" "$DMG_PATH"
mkdir -p "$STAGING"
cp -R "$APP_PATH" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

# Add a small README.
cat > "$STAGING/README.txt" <<EOF
HC3 Menu v${VERSION}

Install:
  1. Drag "HC3 Menu.app" to the Applications folder.
  2. Launch HC3 Menu from Applications. Open Preferences and
     fill in your HC3 host, user, password, and (optionally) PIN.

  3. Allow Local Network access (REQUIRED to reach the HC3).
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
EOF

hdiutil create \
    -volname "HC3 Menu ${VERSION}" \
    -srcfolder "$STAGING" \
    -ov -format UDZO \
    "$DMG_PATH"

rm -rf "$STAGING"

if [[ -n "$NOTARY_PROFILE" && "$SIGN_IDENTITY" != "-" && -n "$SIGN_IDENTITY" ]]; then
    echo ">>> Signing the DMG itself..."
    codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG_PATH"

    echo ">>> Submitting to Apple for notarization (profile: $NOTARY_PROFILE)..."
    echo "    This typically takes 1-3 minutes."
    xcrun notarytool submit "$DMG_PATH" \
        --keychain-profile "$NOTARY_PROFILE" \
        --wait

    echo ">>> Stapling notarization ticket to DMG..."
    xcrun stapler staple "$DMG_PATH"
    xcrun stapler validate "$DMG_PATH"
else
    echo ">>> Skipping notarization (NOTARY_PROFILE empty or ad-hoc signing)."
fi

echo ""
echo "==============================================="
echo "  Built: $DMG_PATH"
SIZE=$(du -h "$DMG_PATH" | cut -f1)
echo "  Size:  $SIZE"
echo "==============================================="
