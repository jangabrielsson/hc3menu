"""NSView containing an NSSlider, for use as an NSMenuItem.view.

A menu item whose `view` is set displays the view in place of the title.
The menu stays open while the user drags the slider; the action fires on
mouse-up because we set continuous=False.
"""
from __future__ import annotations

from typing import Callable

import objc
from AppKit import (
    NSView, NSSlider, NSTextField, NSColor, NSFont, NSMakeRect,
    NSViewWidthSizable,
)
from Foundation import NSObject

# Keep strong refs so PyObjC targets aren't garbage-collected.
# Two-bucket rotation: _TARGETS_PREV holds the most-recently-displayed
# menu's targets (still safe to keep alive), _TARGETS_CURR holds the
# targets for the menu currently being built.  When begin_rebuild() is
# called at the start of each _rebuild_menu(), the previous-previous
# bucket is freed (its menu items have long since been removed from the
# NSMenu hierarchy and no slider will ever fire on them again).
_TARGETS_CURR: list[object] = []
_TARGETS_PREV: list[object] = []


def begin_rebuild() -> None:
    """Call once at the start of each menu rebuild to rotate target buckets.

    Targets from two builds ago are released here — their NSMenuItems were
    already removed from the NSMenu during the intervening rebuild, so no
    slider callback can fire on them.
    """
    global _TARGETS_PREV, _TARGETS_CURR
    _TARGETS_PREV = _TARGETS_CURR   # keep most-recent build's targets alive
    _TARGETS_CURR = []              # new build starts with an empty bucket


class _SliderTarget(NSObject):
    def initWithCallback_label_unit_step_(self, callback, label, unit, step):
        self = objc.super(_SliderTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        self._label = label
        self._unit = unit or "%"
        self._step = int(step) if step else 5
        self._extremes: tuple = ()
        self._last_sent = None
        self._last_seen = None
        return self

    def setExtremes_(self, extremes):  # noqa: N802
        self._extremes = tuple(extremes or ())

    def sliderChanged_(self, sender):
        v = int(round(sender.doubleValue()))
        self._last_seen = v
        if self._label is not None:
            self._label.setStringValue_(f"{v}{self._unit}")
        # Throttle: send only on >=step change, or at the extremes.
        if self._callback is None:
            return
        if (self._last_sent is None
                or abs(v - self._last_sent) >= self._step
                or v in self._extremes):
            self._last_sent = v
            self._callback(v)

    def commit_(self, _sender=None):
        # Called via menu close hook to ensure final value is sent.
        if (self._callback is not None
                and self._last_seen is not None
                and self._last_seen != self._last_sent):
            self._last_sent = self._last_seen
            self._callback(self._last_seen)


def make_slider_view(initial: int, on_change: Callable[[int], None],
                     min_v: int = 0, max_v: int = 100,
                     width: float = 220.0,
                     unit: str = "%",
                     step: int = 5,
                     label_width: float = 56.0) -> NSView:
    height = 28.0
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    container.setAutoresizingMask_(NSViewWidthSizable)

    label = NSTextField.alloc().initWithFrame_(NSMakeRect(width - label_width - 6, 6, label_width, 16))
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAlignment_(2)  # NSTextAlignmentRight
    label.setFont_(NSFont.systemFontOfSize_(11))
    label.setTextColor_(NSColor.secondaryLabelColor())
    label.setStringValue_(f"{int(initial)}{unit}")
    container.addSubview_(label)

    slider = NSSlider.alloc().initWithFrame_(NSMakeRect(8, 4, width - label_width - 20, 20))
    slider.setMinValue_(float(min_v))
    slider.setMaxValue_(float(max_v))
    slider.setDoubleValue_(float(initial))
    # Continuous=True so we get drag events, but we coalesce: the network
    # call is throttled in the target.
    slider.setContinuous_(True)

    target = _SliderTarget.alloc().initWithCallback_label_unit_step_(
        on_change, label, unit, step)
    target.setExtremes_((min_v, max_v))
    _TARGETS_CURR.append(target)  # keep alive until next two rebuilds
    slider.setTarget_(target)
    slider.setAction_("sliderChanged:")
    container.addSubview_(slider)

    return container
