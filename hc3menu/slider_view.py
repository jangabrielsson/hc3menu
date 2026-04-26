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
_TARGETS: list[object] = []


class _SliderTarget(NSObject):
    def initWithCallback_label_(self, callback, label):
        self = objc.super(_SliderTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        self._label = label
        self._last_sent = None
        self._last_seen = None
        return self

    def sliderChanged_(self, sender):
        v = int(round(sender.doubleValue()))
        self._last_seen = v
        if self._label is not None:
            self._label.setStringValue_(f"{v}%")
        # Throttle: send only on >=5 step change, or at the extremes.
        if self._callback is None:
            return
        if (self._last_sent is None
                or abs(v - self._last_sent) >= 5
                or v in (0, 100)):
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
                     width: float = 220.0) -> NSView:
    height = 28.0
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    container.setAutoresizingMask_(NSViewWidthSizable)

    label = NSTextField.alloc().initWithFrame_(NSMakeRect(width - 50, 6, 44, 16))
    label.setBezeled_(False)
    label.setDrawsBackground_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAlignment_(2)  # NSTextAlignmentRight
    label.setFont_(NSFont.systemFontOfSize_(11))
    label.setTextColor_(NSColor.secondaryLabelColor())
    label.setStringValue_(f"{int(initial)}%")
    container.addSubview_(label)

    slider = NSSlider.alloc().initWithFrame_(NSMakeRect(8, 4, width - 64, 20))
    slider.setMinValue_(float(min_v))
    slider.setMaxValue_(float(max_v))
    slider.setDoubleValue_(float(initial))
    # Continuous=True so we get drag events, but we coalesce: the network
    # call is throttled in the target.
    slider.setContinuous_(True)

    target = _SliderTarget.alloc().initWithCallback_label_(on_change, label)
    _TARGETS.append(target)  # keep alive
    slider.setTarget_(target)
    slider.setAction_("sliderChanged:")
    container.addSubview_(slider)

    return container
