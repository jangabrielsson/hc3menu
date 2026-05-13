"""SF Symbols helper for menu items.

`sf_image(name, color=None, template=True)` returns an NSImage suitable for
NSMenuItem.setImage_(...). Falls back to None if the symbol isn't available
on the running macOS.
"""
from __future__ import annotations

from typing import Optional

from AppKit import (
    NSImage, NSColor,
)

try:
    from AppKit import NSImageSymbolConfiguration  # macOS 12+
    _HAS_SYMBOL_CONFIG = True
except ImportError:  # pragma: no cover
    _HAS_SYMBOL_CONFIG = False


# Cache keyed by (name, r, g, b, a, template) so that distinct NSColor
# objects with the same RGBA value share the same cached NSImage.  The old
# key used id(color), which is unique per Python object — so dynamically
# created colors (e.g. colorWithSRGBRed:green:blue:alpha:) never hit the
# cache and it grew without bound (one NSImage per color × per device ×
# per rebuild ≈ gigabytes after days of running).
_CACHE: dict[tuple, NSImage] = {}
_CACHE_MAX = 512  # hard cap; evict oldest half when reached


def _color_key(color: NSColor) -> tuple:
    """Return a stable hashable key for an NSColor."""
    try:
        r = float(color.redComponent())
        g = float(color.greenComponent())
        b = float(color.blueComponent())
        a = float(color.alphaComponent())
        return (round(r, 4), round(g, 4), round(b, 4), round(a, 4))
    except Exception:
        # Fallback for pattern/catalog colors that don't expose RGBA.
        return (id(color),)


def sf_image(name: str,
             color: Optional[NSColor] = None,
             template: bool = True) -> Optional[NSImage]:
    """Return an SF Symbol NSImage, or None if unavailable.

    - `name`: SF Symbol name e.g. "lightbulb.fill"
    - `color`: optional NSColor for palette tint (macOS 12+)
    - `template`: if True and no color, tints with menu text color automatically
    """
    key = (name, _color_key(color) if color is not None else None, template)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None

    if color is not None and _HAS_SYMBOL_CONFIG:
        try:
            cfg = NSImageSymbolConfiguration.configurationWithPaletteColors_([color])
            tinted = img.imageWithSymbolConfiguration_(cfg)
            if tinted is not None:
                img = tinted
        except Exception:
            pass
    elif template:
        img.setTemplate_(True)

    if len(_CACHE) >= _CACHE_MAX:
        # Evict oldest half to keep memory bounded.
        for k in list(_CACHE.keys())[:_CACHE_MAX // 2]:
            del _CACHE[k]
    _CACHE[key] = img
    return img
