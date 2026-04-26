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


_CACHE: dict[tuple, NSImage] = {}


def sf_image(name: str,
             color: Optional[NSColor] = None,
             template: bool = True) -> Optional[NSImage]:
    """Return an SF Symbol NSImage, or None if unavailable.

    - `name`: SF Symbol name e.g. "lightbulb.fill"
    - `color`: optional NSColor for palette tint (macOS 12+)
    - `template`: if True and no color, tints with menu text color automatically
    """
    key = (name, id(color) if color is not None else None, template)
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

    _CACHE[key] = img
    return img
