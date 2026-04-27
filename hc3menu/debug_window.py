"""PyObjC Debug log window: live view of HC3 /debugMessages.

Mirrors the QA log submenu but with scrollback, filter, severity dropdown,
follow-tail, copy/clear, and "Copy QA id" for the selected row.

Reads from `StateStore.recent_debug_messages` on a periodic NSTimer tick so
the window stays in sync with the existing debug-message poller in `app.py`.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import objc
from AppKit import (
    NSApp, NSApplication, NSWindow, NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskMiniaturizable, NSBackingStoreBuffered,
    NSTextField, NSButton, NSButtonTypeSwitch, NSButtonTypeMomentaryPushIn,
    NSBezelStyleRounded, NSPopUpButton, NSScrollView, NSTableView,
    NSTableColumn, NSObject, NSMakeRect, NSPasteboard,
    NSPasteboardTypeString, NSColor, NSFont, NSAlert,
)
from Foundation import NSTimer

from .config import HC3Credentials
from .state import StateStore
from .hc3_client import HC3Client, HC3Error

log = logging.getLogger(__name__)

# Severity filter values (popup index -> set of types accepted).
_SEVERITY_FILTERS = [
    ("All", None),
    ("Errors + warnings", {"error", "warning"}),
    ("Errors only", {"error"}),
    ("Warnings only", {"warning"}),
    ("Trace / debug", {"trace", "debug", "info"}),
]


def _format_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "?"


class _DebugTableSource(NSObject):
    """NSTableView data source backed by the live filtered message list."""

    def initWithRows_(self, rows):  # noqa: N802
        self = objc.super(_DebugTableSource, self).init()
        if self is None:
            return None
        self._rows = list(rows)
        return self

    def setRows_(self, rows):  # noqa: N802
        self._rows = list(rows)

    def rows(self):
        return self._rows

    def numberOfRowsInTableView_(self, tv):  # noqa: N802
        return len(self._rows)

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):  # noqa: N802
        if row < 0 or row >= len(self._rows):
            return ""
        m = self._rows[row]
        ident = str(col.identifier())
        if ident == "time":
            return _format_ts(m.get("timestamp"))
        if ident == "type":
            return str(m.get("type", ""))
        if ident == "tag":
            return str(m.get("tag", ""))
        if ident == "message":
            return str(m.get("message", "")).replace("\n", " ")
        return ""


class DebugLogController:
    """Controller for the Debug log window. Reuse a single instance."""

    def __init__(self,
                 store: StateStore,
                 creds: HC3Credentials,
                 client: Optional[HC3Client] = None):
        self.store = store
        self.creds = creds
        self.client = client
        self._window: Optional[NSWindow] = None
        self._target = None
        self._source: Optional[_DebugTableSource] = None
        self._timer: Optional[NSTimer] = None
        self._fields: dict[str, object] = {}
        self._last_seen_top_id: int = 0
        self._prev_activation_policy: Optional[int] = None

    # -- Public ---------------------------------------------------------
    def show(self) -> None:
        if self._window is None:
            self._target = self._make_target()
            self._window = self._build_window()
        self._refresh_rows(scroll_to_end=True)
        # The app runs as LSUIElement (Accessory, policy=2). To make the
        # window foregroundable and grab focus from the active app we have
        # to flip to Regular (policy=0), activate, then revert when the
        # window closes (see windowWillClose_).
        try:
            app = NSApplication.sharedApplication()
            if self._prev_activation_policy is None:
                self._prev_activation_policy = int(app.activationPolicy())
            app.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular
            app.activateIgnoringOtherApps_(True)
        except Exception:
            log.debug("could not raise activation policy", exc_info=True)
        self._window.makeKeyAndOrderFront_(None)
        try:
            self._window.orderFrontRegardless()
        except Exception:
            pass
        self._start_timer()

    def close(self) -> None:
        self._stop_timer()
        if self._window is not None:
            self._window.close()

    # -- Building -------------------------------------------------------
    def _build_window(self) -> NSWindow:
        rect = NSMakeRect(120, 120, 820, 520)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_("HC3 Menu — Debug log")
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self._target)

        cv = win.contentView()

        # Top toolbar row.
        # Filter field
        filter_lbl = self._label("Filter:", 12, 484, 50)
        cv.addSubview_(filter_lbl)
        filter_tf = NSTextField.alloc().initWithFrame_(NSMakeRect(60, 482, 260, 24))
        filter_tf.setPlaceholderString_("substring (tag or message)")
        filter_tf.setTarget_(self._target)
        filter_tf.setAction_("filterChanged:")
        cv.addSubview_(filter_tf)
        self._fields["filter"] = filter_tf

        # Severity popup
        sev_lbl = self._label("Severity:", 332, 484, 70)
        cv.addSubview_(sev_lbl)
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(400, 480, 180, 26), False
        )
        for label, _ in _SEVERITY_FILTERS:
            popup.addItemWithTitle_(label)
        popup.selectItemAtIndex_(0)
        popup.setTarget_(self._target)
        popup.setAction_("severityChanged:")
        cv.addSubview_(popup)
        self._fields["severity"] = popup

        # Follow tail checkbox
        follow = NSButton.alloc().initWithFrame_(NSMakeRect(594, 482, 110, 22))
        follow.setButtonType_(NSButtonTypeSwitch)
        follow.setTitle_("Follow tail")
        follow.setState_(1)
        cv.addSubview_(follow)
        self._fields["follow"] = follow

        # Refresh button
        refresh = NSButton.alloc().initWithFrame_(NSMakeRect(720, 480, 86, 26))
        refresh.setTitle_("Refresh")
        refresh.setBezelStyle_(NSBezelStyleRounded)
        refresh.setTarget_(self._target)
        refresh.setAction_("refresh:")
        cv.addSubview_(refresh)

        # Table
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 56, 796, 416))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setBorderType_(2)  # NSBezelBorder
        scroll.setAutohidesScrollers_(False)

        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 796, 416))
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsMultipleSelection_(True)
        try:
            table.setFont_(NSFont.userFixedPitchFontOfSize_(11.0))
        except Exception:
            pass

        for ident, title, width in [
            ("time", "Time", 150),
            ("type", "Type", 70),
            ("tag", "Tag", 140),
            ("message", "Message", 420),
        ]:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            table.addTableColumn_(col)

        self._source = _DebugTableSource.alloc().initWithRows_([])
        table.setDataSource_(self._source)
        scroll.setDocumentView_(table)
        cv.addSubview_(scroll)
        self._fields["table"] = table
        self._fields["scroll"] = scroll

        # Bottom buttons
        copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(12, 14, 110, 30))
        copy_btn.setTitle_("Copy selected")
        copy_btn.setBezelStyle_(NSBezelStyleRounded)
        copy_btn.setTarget_(self._target)
        copy_btn.setAction_("copySelected:")
        cv.addSubview_(copy_btn)

        copy_all = NSButton.alloc().initWithFrame_(NSMakeRect(128, 14, 90, 30))
        copy_all.setTitle_("Copy all")
        copy_all.setBezelStyle_(NSBezelStyleRounded)
        copy_all.setTarget_(self._target)
        copy_all.setAction_("copyAll:")
        cv.addSubview_(copy_all)

        open_qa = NSButton.alloc().initWithFrame_(NSMakeRect(228, 14, 130, 30))
        open_qa.setTitle_("Copy QA id")
        open_qa.setBezelStyle_(NSBezelStyleRounded)
        open_qa.setTarget_(self._target)
        open_qa.setAction_("openQA:")
        cv.addSubview_(open_qa)

        clear_btn = NSButton.alloc().initWithFrame_(NSMakeRect(620, 14, 90, 30))
        clear_btn.setTitle_("Clear (HC3)")
        clear_btn.setBezelStyle_(NSBezelStyleRounded)
        clear_btn.setTarget_(self._target)
        clear_btn.setAction_("clearHC3:")
        cv.addSubview_(clear_btn)

        close_btn = NSButton.alloc().initWithFrame_(NSMakeRect(716, 14, 90, 30))
        close_btn.setTitle_("Close")
        close_btn.setBezelStyle_(NSBezelStyleRounded)
        close_btn.setTarget_(self._target)
        close_btn.setAction_("closeWindow:")
        cv.addSubview_(close_btn)

        return win

    def _label(self, text: str, x: float, y: float, w: float) -> NSTextField:
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
        lbl.setStringValue_(text)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        return lbl

    # -- Filtering / refresh -------------------------------------------
    def _current_filter_text(self) -> str:
        tf = self._fields.get("filter")
        if tf is None:
            return ""
        return str(tf.stringValue() or "").strip().lower()

    def _current_severity_set(self) -> Optional[set]:
        popup = self._fields.get("severity")
        if popup is None:
            return None
        idx = int(popup.indexOfSelectedItem())
        if 0 <= idx < len(_SEVERITY_FILTERS):
            return _SEVERITY_FILTERS[idx][1]
        return None

    def _build_filtered_rows(self) -> list[dict]:
        # Pull a generous slice so filtering still has lots to work with.
        all_msgs = self.store.recent_debug_messages(2000)
        sev = self._current_severity_set()
        text = self._current_filter_text()

        def ok(m: dict) -> bool:
            if sev is not None and str(m.get("type", "")).lower() not in sev:
                return False
            if text:
                if (text not in str(m.get("tag", "")).lower()
                        and text not in str(m.get("message", "")).lower()):
                    return False
            return True

        # Reverse so oldest is at top, newest at bottom (natural log order).
        return list(reversed([m for m in all_msgs if ok(m)]))

    def _refresh_rows(self, *, scroll_to_end: bool = False) -> None:
        if self._source is None or self._window is None:
            return
        rows = self._build_filtered_rows()
        self._source.setRows_(rows)
        table = self._fields.get("table")
        if table is not None:
            table.reloadData()
            if scroll_to_end and rows:
                table.scrollRowToVisible_(len(rows) - 1)

    def _on_timer_tick(self) -> None:
        # Only auto-update if window is visible.
        if self._window is None or not self._window.isVisible():
            return
        # Detect if there are new messages by checking the topmost id from store.
        latest = self.store.recent_debug_messages(1)
        if latest:
            try:
                top_id = int(latest[0].get("id", 0))
            except (TypeError, ValueError):
                top_id = 0
        else:
            top_id = 0
        if top_id == self._last_seen_top_id:
            return
        self._last_seen_top_id = top_id
        follow = self._fields.get("follow")
        scroll_end = bool(follow and follow.state())
        self._refresh_rows(scroll_to_end=scroll_end)

    def _start_timer(self) -> None:
        self._stop_timer()
        self._timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.5, self._target, "tick:", None, True
        )

    def _stop_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.invalidate()
            except Exception:
                pass
            self._timer = None

    # -- Actions --------------------------------------------------------
    def _selected_rows(self) -> list[dict]:
        table = self._fields.get("table")
        if table is None or self._source is None:
            return []
        rows = self._source.rows()
        idx_set = table.selectedRowIndexes()
        out: list[dict] = []
        if idx_set is None or rows == []:
            return out
        i = idx_set.firstIndex()
        # NSNotFound = NSUIntegerMax; iterate while valid
        while i < len(rows):
            out.append(rows[i])
            i = idx_set.indexGreaterThanIndex_(i)
            if i >= 2**63:  # NSNotFound sentinel
                break
        return out

    def _format_for_clipboard(self, msgs: list[dict]) -> str:
        lines = []
        for m in msgs:
            lines.append("{ts}\t{type}\t{tag}\t{msg}".format(
                ts=_format_ts(m.get("timestamp")),
                type=m.get("type", ""),
                tag=m.get("tag", ""),
                msg=str(m.get("message", "")).replace("\n", " "),
            ))
        return "\n".join(lines)

    def _put_on_clipboard(self, text: str) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    def _do_copy_selected(self) -> None:
        sel = self._selected_rows()
        if not sel:
            return
        self._put_on_clipboard(self._format_for_clipboard(sel))

    def _do_copy_all(self) -> None:
        rows = self._source.rows() if self._source else []
        if not rows:
            return
        self._put_on_clipboard(self._format_for_clipboard(rows))

    def _do_clear_hc3(self) -> None:
        if self.client is None:
            try:
                self.client = HC3Client(self.creds, request_timeout=5)
            except Exception:
                log.exception("could not build HC3 client for clear")
                return
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Clear HC3 debug messages?")
        alert.setInformativeText_(
            "This deletes the debug messages on the HC3 itself "
            "(DELETE /debugMessages). The local cache will refill on the next "
            "poll cycle.")
        alert.addButtonWithTitle_("Clear")
        alert.addButtonWithTitle_("Cancel")
        if int(alert.runModal()) != 1000:  # NSAlertFirstButtonReturn
            return
        try:
            self.client._request("DELETE", "/debugMessages")
        except HC3Error as e:
            log.warning("clear /debugMessages failed: %s", e)
        # Local store is append-only with id dedupe; nothing to wipe locally.

    def _do_open_qa(self) -> None:
        """Resolve the selected row's tag to a QA id and copy it to clipboard.

        The HC3 web UI has no stable per-QuickApp URL, so opening one in
        the browser isn't reliable. Copying the id lets the user paste it
        into Swagger, scripts, or the HC3 UI search.
        """
        sel = self._selected_rows()
        if not sel:
            return
        msg = sel[0]
        tag = str(msg.get("tag") or "").strip()
        if not tag:
            self._alert("No tag", "This message has no tag to map to a QA.")
            return
        qa_id = self._resolve_qa_id_from_tag(tag)
        if qa_id is None:
            self._alert("QA not found",
                        f"Could not resolve tag '{tag}' to a QuickApp id "
                        "in the local device cache.")
            return
        self._put_on_clipboard(str(qa_id))
        self._alert("Copied",
                    f"QA id {qa_id} copied to clipboard "
                    f"(tag '{tag}').")

    def _ui_locale(self) -> str:
        # Kept for future use; HC3 UI exposes no stable per-QA URL today.
        return "en"

    def _resolve_qa_id_from_tag(self, tag: str) -> Optional[int]:
        # Numeric tag.
        try:
            return int(tag)
        except ValueError:
            pass
        # "QUICKAPP123" style.
        import re
        m = re.search(r"(\d+)", tag)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        # Match against device name.
        for d in self.store.all_devices():
            if str(d.get("name", "")).strip() == tag:
                try:
                    return int(d.get("id"))
                except (TypeError, ValueError):
                    pass
        return None

    def _alert(self, title: str, body: str) -> None:
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(body)
        a.runModal()

    # -- Objective-C target ---------------------------------------------
    def _make_target(self):
        outer = self

        class _Target(NSObject):
            def filterChanged_(self, sender):  # noqa: N802
                outer._refresh_rows(scroll_to_end=False)

            def severityChanged_(self, sender):  # noqa: N802
                outer._refresh_rows(scroll_to_end=True)

            def refresh_(self, sender):  # noqa: N802
                outer._refresh_rows(scroll_to_end=True)

            def copySelected_(self, sender):  # noqa: N802
                outer._do_copy_selected()

            def copyAll_(self, sender):  # noqa: N802
                outer._do_copy_all()

            def clearHC3_(self, sender):  # noqa: N802
                outer._do_clear_hc3()

            def openQA_(self, sender):  # noqa: N802
                outer._do_open_qa()

            def closeWindow_(self, sender):  # noqa: N802
                outer.close()

            def tick_(self, _timer):  # noqa: N802
                outer._on_timer_tick()

            # NSWindowDelegate
            def windowWillClose_(self, _note):  # noqa: N802
                outer._stop_timer()
                # Restore Accessory policy so we vanish from Cmd-Tab again.
                if outer._prev_activation_policy is not None:
                    try:
                        NSApplication.sharedApplication().setActivationPolicy_(
                            outer._prev_activation_policy
                        )
                    except Exception:
                        pass
                    outer._prev_activation_policy = None

        return _Target.alloc().init()
