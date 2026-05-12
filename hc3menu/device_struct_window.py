"""Floating panel that shows a device's JSON struct with a Copy button."""
from __future__ import annotations

import json
import logging
from typing import Optional

import objc
from AppKit import (
    NSApp, NSApplication, NSPanel,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
    NSBackingStoreBuffered, NSScrollView, NSTextView,
    NSButton, NSTextField, NSMakeRect,
    NSColor, NSFont, NSPasteboard, NSStringPboardType,
    NSBezelStyleRounded, NSTextAlignmentLeft,
)
from Foundation import NSObject, NSString

log = logging.getLogger(__name__)

# Module-level: keep the panel alive (strong ref) so it isn't GC'd.
_panel: Optional[NSPanel] = None
_text_view: Optional[NSTextView] = None
_title_label: Optional[NSTextField] = None
_btn_target = None  # keep ObjC target alive
_prev_policy: Optional[int] = None


def show_device_struct(device: dict) -> None:
    """Open (or reuse) the floating JSON panel for *device*."""
    global _panel, _text_view, _title_label, _prev_policy

    if _panel is None:
        _panel, _text_view, _title_label = _build_panel()

    # Fill in content
    name = device.get("name") or f"Device {device.get('id', '?')}"
    _title_label.setStringValue_(f"{name}  (id {device.get('id', '?')})")
    pretty = json.dumps(device, indent=2, ensure_ascii=False)
    _text_view.setString_(pretty)
    # Scroll to top
    _text_view.scrollRangeToVisible_((0, 0))

    # Promote to regular app so the panel can receive focus
    try:
        app = NSApplication.sharedApplication()
        if _prev_policy is None:
            _prev_policy = int(app.activationPolicy())
        app.setActivationPolicy_(0)
        app.activateIgnoringOtherApps_(True)
    except Exception:
        log.debug("could not raise activation policy", exc_info=True)

    _panel.makeKeyAndOrderFront_(None)
    try:
        _panel.orderFrontRegardless()
    except Exception:
        pass


def _copy_json(_sender) -> None:
    if _text_view is None:
        return
    text = str(_text_view.string())
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSStringPboardType)


def _close_panel(_sender) -> None:
    global _prev_policy
    if _panel is not None:
        _panel.orderOut_(None)
    # Restore LSUIElement policy
    try:
        app = NSApplication.sharedApplication()
        if _prev_policy is not None:
            app.setActivationPolicy_(_prev_policy)
            _prev_policy = None
    except Exception:
        pass


def _build_panel():
    global _btn_target
    W, H = 640, 480

    style = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable
        | NSWindowStyleMaskMiniaturizable
    )
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(200, 200, W, H), style, NSBackingStoreBuffered, False
    )
    panel.setTitle_("Device struct")
    panel.setReleasedWhenClosed_(False)
    panel.setLevel_(3)  # NSFloatingWindowLevel

    cv = panel.contentView()
    cv.setAutoresizesSubviews_(True)

    BUTTON_H = 32
    PADDING = 10
    TITLE_H = 22

    # -- Title label (device name + id) --
    from AppKit import NSViewMinYMargin, NSViewWidthSizable
    title_label = NSTextField.alloc().initWithFrame_(
        NSMakeRect(PADDING, H - PADDING - TITLE_H, W - 2 * PADDING, TITLE_H)
    )
    title_label.setStringValue_("")
    title_label.setBezeled_(False)
    title_label.setDrawsBackground_(False)
    title_label.setEditable_(False)
    title_label.setSelectable_(False)
    title_label.setFont_(NSFont.boldSystemFontOfSize_(12))
    title_label.setAutoresizingMask_(NSViewWidthSizable | NSViewMinYMargin)
    cv.addSubview_(title_label)

    # -- Scroll + text view --
    scroll_top = PADDING + BUTTON_H + PADDING
    scroll_h = H - scroll_top - PADDING - TITLE_H - PADDING
    from AppKit import NSViewWidthSizable, NSViewHeightSizable
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(PADDING, scroll_top, W - 2 * PADDING, scroll_h)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(True)
    scroll.setAutohidesScrollers_(True)
    scroll.setBorderType_(2)  # NSBezelBorder
    scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

    text_view = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, W - 2 * PADDING, scroll_h)
    )
    text_view.setEditable_(False)
    text_view.setSelectable_(True)
    text_view.setRichText_(False)
    text_view.setFont_(NSFont.fontWithName_size_("Menlo", 11))
    text_view.setTextColor_(NSColor.labelColor())
    text_view.setBackgroundColor_(NSColor.textBackgroundColor())
    text_view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    scroll.setDocumentView_(text_view)
    cv.addSubview_(scroll)

    # -- Buttons (Copy + Close) --
    from AppKit import NSViewMaxYMargin
    BTN_W = 100
    copy_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(PADDING, PADDING, BTN_W, BUTTON_H)
    )
    copy_btn.setTitle_("Copy JSON")
    copy_btn.setBezelStyle_(NSBezelStyleRounded)
    copy_btn.setAutoresizingMask_(NSViewMaxYMargin)

    # Use a raw block as action target via ObjC
    copy_btn.setTarget_(None)
    copy_btn.setAction_(None)

    close_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(W - PADDING - BTN_W, PADDING, BTN_W, BUTTON_H)
    )
    close_btn.setTitle_("Close")
    close_btn.setBezelStyle_(NSBezelStyleRounded)
    close_btn.setAutoresizingMask_(NSViewMaxYMargin)
    close_btn.setKeyEquivalent_("\x1b")  # Escape closes

    # Wire buttons via a small ObjC helper target
    target = _BtnTarget.alloc().init()
    target._text_view_ref = text_view
    target._panel_ref = panel
    copy_btn.setTarget_(target)
    copy_btn.setAction_("copyJSON:")
    close_btn.setTarget_(target)
    close_btn.setAction_("closePanel:")

    # Keep the target alive via module-level ref (can't set attrs on NSPanel)
    _btn_target = target

    cv.addSubview_(copy_btn)
    cv.addSubview_(close_btn)

    return panel, text_view, title_label


class _BtnTarget(NSObject):
    _text_view_ref = None
    _panel_ref = None

    def copyJSON_(self, _sender):  # noqa: N802
        if self._text_view_ref is None:
            return
        text = str(self._text_view_ref.string())
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSStringPboardType)

    def closePanel_(self, _sender):  # noqa: N802
        global _prev_policy
        if self._panel_ref is not None:
            self._panel_ref.orderOut_(None)
        try:
            app = NSApplication.sharedApplication()
            if _prev_policy is not None:
                app.setActivationPolicy_(_prev_policy)
                _prev_policy = None
        except Exception:
            pass
