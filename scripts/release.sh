#!/usr/bin/env bash
# Cut a GitHub release for HC3 Menu.
#
# Workflow:
#   1. Bump hc3menu/__version__.py (manually) and commit it.
#   2. Run ./scripts/release.sh
#      - Verifies clean working tree.
#      - Verifies tag does not already exist.
#      - Builds arm64 DMG (always).
#      - Builds x86_64 DMG if BUILD_X86_64 is not set to 0 and a
#        .venv-x86_64 venv exists (or Rosetta 2 is available).
#      - Creates and pushes git tag v<version>.
#      - Creates GitHub release with auto-generated notes and DMG assets.
#
# Requirements:
#   - gh CLI (brew install gh) authenticated to github.com.
#   - Push access to the repo.
#
# Intel (x86_64) build requirements (on Apple Silicon):
#   - Rosetta 2 installed  (softwareupdate --install-rosetta)
#   - .venv-x86_64 venv with project deps + py2app
#       arch -x86_64 python3 -m venv .venv-x86_64
#       source .venv-x86_64/bin/activate && pip install -e . py2app
#
# To skip the Intel build:
#   BUILD_X86_64=0 ./scripts/release.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# Activate venv if present.
if [[ -z "${VIRTUAL_ENV:-}" && -d ".venv" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

VERSION="$(python -c 'from hc3menu.__version__ import __version__; print(__version__)')"
TAG="v${VERSION}"
DMG_ARM64="dist/HC3-Menu-${VERSION}-arm64.dmg"
DMG_X86_64="dist/HC3-Menu-${VERSION}-x86_64.dmg"

echo ">>> Releasing HC3 Menu ${TAG}"

# Pre-flight checks.
command -v gh >/dev/null 2>&1 || {
    echo "ERROR: 'gh' CLI not found. Install with: brew install gh"
    exit 1
}

if [[ -n "$(git status --porcelain)" ]]; then
    echo "ERROR: working tree is dirty. Commit or stash changes first."
    git status --short
    exit 1
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "ERROR: tag $TAG already exists. Bump hc3menu/__version__.py first."
    exit 1
fi

if gh release view "$TAG" >/dev/null 2>&1; then
    echo "ERROR: GitHub release $TAG already exists."
    exit 1
fi

# Decide whether to build the Intel version.
# Skip if BUILD_X86_64=0 or if Rosetta / x86_64 Python are unavailable.
BUILD_X86_64="${BUILD_X86_64:-1}"
if [[ "$BUILD_X86_64" == "1" ]]; then
    if ! arch -x86_64 true 2>/dev/null; then
        echo ">>> WARNING: Rosetta 2 not available — skipping x86_64 build."
        BUILD_X86_64=0
    fi
fi

# Build arm64 DMG.
echo ">>> Building arm64 DMG..."
TARGET_ARCH=arm64 ./scripts/build_dmg.sh

if [[ ! -f "$DMG_ARM64" ]]; then
    echo "ERROR: expected $DMG_ARM64 not found after arm64 build."
    exit 1
fi

# Build x86_64 DMG.
if [[ "$BUILD_X86_64" == "1" ]]; then
    echo ">>> Building x86_64 DMG..."
    VIRTUAL_ENV="" TARGET_ARCH=x86_64 ./scripts/build_dmg.sh

    if [[ ! -f "$DMG_X86_64" ]]; then
        echo "ERROR: expected $DMG_X86_64 not found after x86_64 build."
        exit 1
    fi
else
    echo ">>> Skipping x86_64 build (BUILD_X86_64=${BUILD_X86_64})."
fi

# Tag and push.
echo ">>> Creating tag $TAG"
git tag -a "$TAG" -m "Release $TAG"
git push origin "$TAG"

# Gather DMG assets to attach to the release.
RELEASE_ASSETS=("$DMG_ARM64")
[[ "$BUILD_X86_64" == "1" ]] && RELEASE_ASSETS+=("$DMG_X86_64")

# Create the GitHub release.
echo ">>> Creating GitHub release $TAG"
gh release create "$TAG" \
    --title "HC3 Menu ${TAG}" \
    --generate-notes \
    "${RELEASE_ASSETS[@]}"

echo ""
echo "==============================================="
echo "  Released: $TAG"
for asset in "${RELEASE_ASSETS[@]}"; do
    SIZE=$(du -h "$asset" | cut -f1)
    echo "  Asset:    $asset  ($SIZE)"
done
echo "  URL:      $(gh release view "$TAG" --json url -q .url)"
echo "==============================================="
