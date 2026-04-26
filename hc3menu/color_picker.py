"""Native macOS color picker (NSColorPanel) wrapper.

Uses the shared NSColorPanel as a non-modal floating panel and a small
NSObject "controller" that receives `colorChanged:` action callbacks.
The controller forwards the picked RGBW values to a user-supplied callable.

Because NSColorPanel is a singleton, only one picker target is active at a
time: opening the picker for a different device replaces the previous target.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from AppKit import (
    NSApp,
    NSColor,
    NSColorPanel,
    NSObject,
)

log = logging.getLogger(__name__)


class _ColorPanelController(NSObject):
    """NSObject target for NSColorPanel `colorChanged:` action."""

    def initWithCallback_(self, callback):  # noqa: N802
        self = NSObject.init(self)
        if self is None:
            return None
        self._callback = callback
        return self

    def colorChanged_(self, sender):  # noqa: N802
        try:
            panel = sender if isinstance(sender, NSColorPanel) else NSColorPanel.sharedColorPanel()
            color = panel.color()
            # Convert to sRGB to get stable 0..1 RGB regardless of source space.
            srgb = color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
            if srgb is None:
                srgb = color
            r = int(round(srgb.redComponent() * 255))
            g = int(round(srgb.greenComponent() * 255))
            b = int(round(srgb.blueComponent() * 255))
            r = max(0, min(255, r))
            g = max(0, min(255, g))
            b = max(0, min(255, b))
            cb = self._callback
            if cb is not None:
                cb(r, g, b)
        except Exception as e:  # noqa: BLE001
            log.exception("NSColorPanel colorChanged_ failed: %s", e)


# Module-level singleton: keep a strong Python reference so PyObjC doesn't
# release the controller while the panel is showing.
_controller: Optional[_ColorPanelController] = None


def show_color_picker(*, title: str,
                      initial_rgb: tuple[int, int, int] = (255, 255, 255),
                      on_pick: Callable[[int, int, int], None]) -> None:
    """Show the shared NSColorPanel and call `on_pick(r, g, b)` on each change.

    The picker stays open until the user closes it; `on_pick` may fire many
    times (every drag in the wheel). Callers should debounce or rate-limit
    if needed.
    """
    global _controller
    _controller = _ColorPanelController.alloc().initWithCallback_(on_pick)

    panel = NSColorPanel.sharedColorPanel()
    panel.setTitle_(title)
    panel.setShowsAlpha_(False)
    panel.setContinuous_(True)
    r, g, b = initial_rgb
    try:
        c = NSColor.colorWithSRGBRed_green_blue_alpha_(
            r / 255.0, g / 255.0, b / 255.0, 1.0)
        panel.setColor_(c)
    except Exception:
        pass
    panel.setTarget_(_controller)
    panel.setAction_("colorChanged:")

    # Make sure the app is active so the panel comes to the front.
    try:
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:
        pass
    panel.makeKeyAndOrderFront_(None)
