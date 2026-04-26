#!/usr/bin/env python
"""Generate assets/icon.icns for HC3 Menu.

Creates a rounded-rectangle blue background, a white house silhouette, and
white "HC3" text. Renders all sizes required by macOS and packs them into
``assets/icon.icns`` using ``iconutil``.

Run from repo root:
    python scripts/make_icon.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.stderr.write("Pillow required: pip install pillow\n")
    sys.exit(1)


REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "assets"
ICONSET = ASSETS / "icon.iconset"
OUT = ASSETS / "icon.icns"

# macOS .iconset members.
SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

# Colors.
BG_TOP = (38, 99, 235)      # blue-600
BG_BOT = (29, 78, 216)      # blue-700
HOUSE = (255, 255, 255)
TEXT = (255, 255, 255)


def _font(size: int) -> ImageFont.FreeTypeFont:
    """Pick a heavy system font; fall back gracefully.

    Each entry is (path, ttc-index). Use index=0 by default; for .ttc
    collections we pick a Bold variant index when known.
    """
    candidates = [
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ("/Library/Fonts/Arial Bold.ttf", 0),
        ("/System/Library/Fonts/Helvetica.ttc", 1),     # Helvetica Bold
        ("/System/Library/Fonts/HelveticaNeue.ttc", 1),
        ("/System/Library/Fonts/SFNSRounded.ttf", 0),
        ("/System/Library/Fonts/SFNS.ttf", 0),
    ]
    for path, idx in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=idx)
            except OSError:
                continue
    return ImageFont.load_default()


def _rounded_gradient(size: int, radius_frac: float = 0.225) -> Image.Image:
    """Vertical blue gradient inside a rounded square (Big Sur style)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # Build gradient first.
    grad = Image.new("RGBA", (1, size), (0, 0, 0, 255))
    for y in range(size):
        t = y / max(1, size - 1)
        r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t)
        grad.putpixel((0, y), (r, g, b, 255))
    grad = grad.resize((size, size))

    # Mask: rounded square.
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    radius = int(size * radius_frac)
    mdraw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)

    img.paste(grad, (0, 0), mask)
    return img


def _draw_house(img: Image.Image) -> None:
    """Draw a filled white house silhouette in the upper half."""
    s = img.size[0]
    d = ImageDraw.Draw(img)

    # Geometry (fractions of canvas size). Roof apex high, body short so the
    # bottom half is free for "HC3".
    cx = s / 2
    apex_y = s * 0.18
    eaves_y = s * 0.40
    base_y = s * 0.58
    half_w = s * 0.30
    body_half = s * 0.24

    # Roof triangle (slightly wider eaves than body).
    roof = [
        (cx, apex_y),
        (cx - half_w, eaves_y),
        (cx + half_w, eaves_y),
    ]
    d.polygon(roof, fill=HOUSE)

    # Body rectangle.
    body = [
        cx - body_half, eaves_y,
        cx + body_half, base_y,
    ]
    d.rectangle(body, fill=HOUSE)

    # Door (cut out by drawing in background blue gradient — easier: punch
    # alpha hole then fill nothing; instead draw with the blue background).
    door_w = s * 0.07
    door_h = s * 0.13
    door = [
        cx - door_w, base_y - door_h,
        cx + door_w, base_y,
    ]
    # Sample a representative blue (mid-gradient) for the door cut-out.
    door_color = tuple(int((a + b) / 2) for a, b in zip(BG_TOP, BG_BOT)) + (255,)
    d.rectangle(door, fill=door_color)


def _draw_text(img: Image.Image) -> None:
    s = img.size[0]
    d = ImageDraw.Draw(img)

    # Pick a font size that fits "HC3" comfortably across ~70% of width.
    target_w = s * 0.66
    size = int(s * 0.32)
    font = _font(size)
    # Tune size so it actually hits target width.
    for _ in range(8):
        bbox = d.textbbox((0, 0), "HC3", font=font)
        w = bbox[2] - bbox[0]
        if w > target_w * 1.05:
            size = int(size * 0.93)
        elif w < target_w * 0.95:
            size = int(size * 1.06)
        else:
            break
        font = _font(size)

    bbox = d.textbbox((0, 0), "HC3", font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (s - w) / 2 - bbox[0]
    y = s * 0.66 - bbox[1]
    # Adjust so text sits centered between house base (~0.58) and bottom edge.
    y = s * 0.62 - bbox[1] + (s * 0.30 - h) / 2

    d.text((x, y), "HC3", fill=TEXT, font=font)


def render(size: int) -> Image.Image:
    img = _rounded_gradient(size)
    _draw_house(img)
    _draw_text(img)
    return img


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir()

    print(f">>> Rendering {len(SIZES)} icon sizes...")
    for name, size in SIZES:
        img = render(size)
        img.save(ICONSET / name, "PNG")
        print(f"    {name}  ({size}x{size})")

    print(">>> Packing with iconutil...")
    if OUT.exists():
        OUT.unlink()
    subprocess.run(
        ["iconutil", "-c", "icns", str(ICONSET), "-o", str(OUT)],
        check=True,
    )
    shutil.rmtree(ICONSET)
    print(f">>> Wrote {OUT}")


if __name__ == "__main__":
    main()
