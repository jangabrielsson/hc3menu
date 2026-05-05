"""GitHub release update checker for hc3menu.

Hits ``https://api.github.com/repos/<slug>/releases/latest`` and compares the
returned ``tag_name`` (stripped of a leading ``v``) against the bundled
``__version__``. Returns a small dataclass the UI can use.

No third-party deps beyond ``requests`` (already used by the HC3 client).
"""
from __future__ import annotations

import logging
import platform
import re
from dataclasses import dataclass
from typing import Optional

import requests

from .__version__ import __version__

log = logging.getLogger(__name__)

GITHUB_REPO = "jangabrielsson/hc3menu"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


@dataclass
class UpdateInfo:
    current: str
    latest: str
    is_newer: bool
    html_url: str
    download_url: Optional[str]  # first .dmg asset, if any
    notes: str


def _parse_semver(s: str) -> tuple[int, ...]:
    """Best-effort semver parse: '1.2.3' -> (1, 2, 3). Trailing junk ignored."""
    s = s.strip().lstrip("v").lstrip("V")
    parts = re.split(r"[.\-+]", s)
    out: list[int] = []
    for p in parts:
        m = re.match(r"^\d+", p)
        if not m:
            break
        out.append(int(m.group(0)))
    return tuple(out) or (0,)


def check_for_update(timeout: float = 6.0) -> Optional[UpdateInfo]:
    """Return an :class:`UpdateInfo` or ``None`` on network/parse failure."""
    try:
        r = requests.get(
            RELEASES_URL,
            timeout=timeout,
            headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.info("update check failed: %s", e)
        return None

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return None
    latest = tag.lstrip("vV")
    current = __version__
    is_newer = _parse_semver(latest) > _parse_semver(current)

    # Prefer the DMG that matches the running architecture (arm64 / x86_64).
    machine = platform.machine()  # 'arm64' on Apple Silicon, 'x86_64' on Intel
    dmg_url: Optional[str] = None
    fallback_url: Optional[str] = None
    for asset in data.get("assets") or []:
        n = str(asset.get("name") or "")
        if not n.lower().endswith(".dmg"):
            continue
        url = asset.get("browser_download_url")
        if machine in n:
            dmg_url = url
            break
        if fallback_url is None:
            fallback_url = url
    if dmg_url is None:
        dmg_url = fallback_url

    return UpdateInfo(
        current=current,
        latest=latest,
        is_newer=is_newer,
        html_url=str(data.get("html_url") or RELEASES_PAGE),
        download_url=dmg_url,
        notes=str(data.get("body") or ""),
    )
