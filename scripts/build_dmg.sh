#!/usr/bin/env bash
# Build a standalone .app and .dmg for HC3 Menu.
#
# Output: dist/HC3-Menu-<version>-<arch>.dmg
#
# Requirements:
#   - macOS (arm64 or x86_64)
#   - Python venv with project deps + py2app installed
#   - hdiutil (built-in)
#
# Architecture:
#   By default builds for the current machine architecture.
#   Override via TARGET_ARCH env var (arm64 or x86_64).
#
#   Cross-compiling for x86_64 on Apple Silicon requires:
#     - Rosetta 2 installed  (softwareupdate --install-rosetta)
#     - An x86_64 Python installation and venv at .venv-x86_64
#       (create with: arch -x86_64 python3 -m venv .venv-x86_64
#                      source .venv-x86_64/bin/activate
#                      pip install -e . py2app)
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
#   ./scripts/build_dmg.sh                              # native arch, signed + notarized
#   TARGET_ARCH=x86_64 ./scripts/build_dmg.sh           # Intel build (cross-compile)
#   SIGN_IDENTITY=- NOTARY_PROFILE= ./scripts/build_dmg.sh   # ad-hoc, no notarize
set -euo pipefail

cd "$(dirname "$0")/.."

# Detect native architecture and resolve target.
NATIVE_ARCH="$(uname -m)"
TARGET_ARCH="${TARGET_ARCH:-$NATIVE_ARCH}"

if [[ "$TARGET_ARCH" != "arm64" && "$TARGET_ARCH" != "x86_64" ]]; then
    echo "ERROR: unsupported TARGET_ARCH '$TARGET_ARCH'. Use 'arm64' or 'x86_64'."
    exit 1
fi

# When cross-compiling (e.g. arm64 host → x86_64 target) we need Rosetta 2
# and use 'arch -x86_64' to run Python in Intel mode.
ARCH_PREFIX=""
if [[ "$TARGET_ARCH" != "$NATIVE_ARCH" ]]; then
    if ! arch -"$TARGET_ARCH" true 2>/dev/null; then
        echo "ERROR: cannot run $TARGET_ARCH binaries on this host."
        echo "  For x86_64 cross-builds, install Rosetta 2:"
        echo "    softwareupdate --install-rosetta"
        exit 1
    fi
    ARCH_PREFIX="arch -$TARGET_ARCH"
    echo ">>> Cross-compiling: native=$NATIVE_ARCH  target=$TARGET_ARCH"
fi

# Pick the appropriate venv:
#   arm64  → .venv  (default)
#   x86_64 → .venv-x86_64  (must be created manually for cross-builds)
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ "$TARGET_ARCH" == "x86_64" && -d ".venv-x86_64" ]]; then
        # shellcheck disable=SC1091
        source .venv-x86_64/bin/activate
    elif [[ -d ".venv" ]]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate
    fi
fi

# Verify the active Python has the right arch.
PYTHON_ARCH="$(${ARCH_PREFIX} python -c 'import platform; print(platform.machine())' 2>/dev/null || true)"
if [[ -n "$PYTHON_ARCH" && "$PYTHON_ARCH" != "$TARGET_ARCH" ]]; then
    echo "WARNING: active Python reports arch '$PYTHON_ARCH' but TARGET_ARCH='$TARGET_ARCH'."
    echo "  For x86_64 builds create a dedicated venv:"
    echo "    arch -x86_64 python3 -m venv .venv-x86_64"
    echo "    source .venv-x86_64/bin/activate && pip install -e . py2app"
    echo "  Then re-run: TARGET_ARCH=x86_64 ./scripts/build_dmg.sh"
    exit 1
fi

# Read version from the package.
VERSION="$(${ARCH_PREFIX} python -c 'from hc3menu.__version__ import __version__; print(__version__)')"
echo ">>> Building HC3 Menu v${VERSION} (${TARGET_ARCH})"

# Ensure py2app is available.
${ARCH_PREFIX} python -c "import py2app" 2>/dev/null || {
    echo ">>> Installing py2app..."
    ${ARCH_PREFIX} pip install py2app
}

# Clean py2app's working directory but preserve dist/ so multiple arch
# DMGs can accumulate there side by side.
rm -rf build dist/HC3\ Menu.app

echo ">>> Running py2app..."
${ARCH_PREFIX} python setup.py py2app

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

    # py2app bundles ship hundreds of pre-signed (ad-hoc) Mach-O files
    # under Contents/Resources/lib/.../*.so. `codesign --deep` does NOT
    # re-sign already-signed binaries, so we must walk the bundle and
    # re-sign each Mach-O explicitly with hardened runtime + timestamp.
    echo "    Finding nested Mach-O binaries..."
    NESTED=()
    while IFS= read -r -d '' f; do
        # Filter to actual Mach-O binaries.
        if file -b "$f" | grep -q "Mach-O"; then
            NESTED+=("$f")
        fi
    done < <(find "$APP_PATH/Contents" \
        \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) \
        -type f -print0)
    echo "    Re-signing ${#NESTED[@]} nested binaries..."
    for f in "${NESTED[@]}"; do
        codesign --force --options runtime --timestamp \
            --sign "$SIGN_IDENTITY" "$f"
    done

    # Also sign any frameworks inside (deepest first — Versions/A then the
    # framework directory itself). py2app embeds Python.framework.
    if [[ -d "$APP_PATH/Contents/Frameworks" ]]; then
        echo "    Re-signing frameworks..."
        # Sign nested executables inside frameworks first.
        find "$APP_PATH/Contents/Frameworks" -type f -perm -u+x \
            ! -name "*.py" ! -name "*.pyc" -print0 |
        while IFS= read -r -d '' f; do
            if file -b "$f" | grep -q "Mach-O"; then
                codesign --force --options runtime --timestamp \
                    --sign "$SIGN_IDENTITY" "$f" 2>/dev/null || true
            fi
        done
        # Then the framework bundles themselves (sorted deepest first).
        find "$APP_PATH/Contents/Frameworks" -name "*.framework" -type d -print0 |
            sort -rz |
            while IFS= read -r -d '' fw; do
                codesign --force --options runtime --timestamp \
                    --sign "$SIGN_IDENTITY" "$fw" 2>/dev/null || true
            done
    fi

    # Sign every Mach-O executable in Contents/MacOS/ (py2app drops both
    # the launcher and a `python` interpreter binary here).
    echo "    Signing Contents/MacOS/ executables..."
    for f in "$APP_PATH/Contents/MacOS/"*; do
        [[ -f "$f" ]] || continue
        if file -b "$f" | grep -q "Mach-O"; then
            codesign --force --options runtime --timestamp \
                --entitlements "$ENTITLEMENTS" \
                --sign "$SIGN_IDENTITY" "$f"
        fi
    done

    # Finally sign the outer .app with hardened runtime + entitlements.
    echo "    Signing outer .app bundle with entitlements..."
    codesign --force --options runtime --timestamp \
        --entitlements "$ENTITLEMENTS" \
        --sign "$SIGN_IDENTITY" \
        "$APP_PATH"

    echo ">>> Verifying signature..."
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
fi

DMG_NAME="HC3-Menu-${VERSION}-${TARGET_ARCH}.dmg"
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
