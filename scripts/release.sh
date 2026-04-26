#!/usr/bin/env bash
# Cut a GitHub release for HC3 Menu.
#
# Workflow:
#   1. Bump hc3menu/__version__.py (manually) and commit it.
#   2. Run ./scripts/release.sh
#      - Verifies clean working tree.
#      - Verifies tag does not already exist.
#      - Builds arm64 DMG.
#      - Creates and pushes git tag v<version>.
#      - Creates GitHub release with auto-generated notes and DMG asset.
#
# Requirements:
#   - gh CLI (brew install gh) authenticated to github.com.
#   - Push access to the repo.
set -euo pipefail

cd "$(dirname "$0")/.."

# Activate venv if present.
if [[ -z "${VIRTUAL_ENV:-}" && -d ".venv" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

VERSION="$(python -c 'from hc3menu.__version__ import __version__; print(__version__)')"
TAG="v${VERSION}"
DMG_PATH="dist/HC3-Menu-${VERSION}-arm64.dmg"

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

# Build the DMG.
./scripts/build_dmg.sh

if [[ ! -f "$DMG_PATH" ]]; then
    echo "ERROR: expected $DMG_PATH not found after build."
    exit 1
fi

# Tag and push.
echo ">>> Creating tag $TAG"
git tag -a "$TAG" -m "Release $TAG"
git push origin "$TAG"

# Create the GitHub release.
echo ">>> Creating GitHub release $TAG"
gh release create "$TAG" \
    --title "HC3 Menu ${TAG}" \
    --generate-notes \
    "$DMG_PATH"

echo ""
echo "==============================================="
echo "  Released: $TAG"
echo "  Asset:    $DMG_PATH"
echo "  URL:      $(gh release view "$TAG" --json url -q .url)"
echo "==============================================="
