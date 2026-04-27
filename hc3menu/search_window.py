"""Spotlight-style search window for devices and scenes.

Opens a small floating window with a search field at the top and a results
table below. Type to filter (subsequence fuzzy match), Up/Down to navigate,
Enter to activate (toggle a switch/dimmer, run a scene), Esc to close.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import objc
from AppKit import (
    NSApp, NSApplication, NSWindow, NSPanel, NSButton,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskMiniaturizable,
    NSBackingStoreBuffered, NSSearchField, NSTextField,
    NSScrollView, NSTableView, NSTableColumn, NSObject, NSMakeRect,
    NSColor, NSFont, NSImage, NSImageView, NSImageScaleProportionallyUpOrDown,
    NSBezelStyleRounded,
)
from Foundation import NSObject as _NSObject

from .state import StateStore
from .config import HC3Credentials

log = logging.getLogger(__name__)


# -- Action classification ---------------------------------------------
# Map a device classification to (action_label, activate_kind).
#   activate_kind ∈ {"toggle", "shutter", "open_in_hc3"}
_DEVICE_ACTIONS: dict[Optional[str], tuple[str, str]] = {
    "switch":          ("Toggle",        "toggle"),
    "dimmer":          ("Toggle",        "toggle"),
    "shutter":         ("Open / Close",  "shutter"),
    "temp_sensor":     ("Open in HC3",   "open_in_hc3"),
    "lux_sensor":      ("Open in HC3",   "open_in_hc3"),
    "humidity_sensor": ("Open in HC3",   "open_in_hc3"),
    "motion_sensor":   ("Open in HC3",   "open_in_hc3"),
    "thermostat":      ("Open in HC3",   "open_in_hc3"),
    None:              ("Open in HC3",   "open_in_hc3"),
}


def _device_action(device: dict) -> tuple[str, str]:
    """Return (label, kind) describing what Enter should do for `device`."""
    from .menu_builder import classify  # local import to avoid cycles
    cls = classify(device)
    # Color-controllers are dimmers under the hood — toggle them.
    if cls is None:
        actions = device.get("actions") or {}
        if isinstance(actions, dict) and ("turnOn" in actions or "turnOff" in actions):
            return ("Toggle", "toggle")
    return _DEVICE_ACTIONS.get(cls, ("Open in HC3", "open_in_hc3"))


def _fuzzy_score(query: str, candidate: str) -> Optional[int]:
    """Subsequence fuzzy score. Returns None if no match.

    Higher is better. Heuristics:
    - Each character of `query` must appear in `candidate` in order.
    - Tighter spans score higher.
    - Word-start matches (after space / underscore / start) get a bonus.
    - Exact prefix match gets the biggest bonus.
    """
    if not query:
        return 0
    q = query.lower()
    c = candidate.lower()
    if not c:
        return None
    # Quick prefix bonus.
    if c.startswith(q):
        return 1000 + (100 - min(len(c), 100))

    score = 0
    last_idx = -1
    last_char = ""
    for ch in q:
        idx = c.find(ch, last_idx + 1)
        if idx < 0:
            return None
        # Penalty for distance from previous match.
        gap = idx - last_idx - 1 if last_idx >= 0 else idx
        score -= gap
        # Bonus: match at word start.
        if idx == 0 or c[idx - 1] in (" ", "_", "-", "/", "."):
            score += 10
        # Bonus: consecutive char.
        if last_idx == idx - 1:
            score += 5
        last_idx = idx
        last_char = ch
    # Shorter candidates beat longer ones at same fit.
    score -= len(c) // 4
    return score


def _build_index(store: StateStore) -> list[dict]:
    """Flatten devices + scenes into a single searchable index list."""
    rows: list[dict] = []
    # Rooms lookup
    for d in store.all_devices():
        try:
            did = int(d.get("id", 0))
        except (TypeError, ValueError):
            continue
        if did <= 3:  # HC3 reserves 1..3 for system
            continue
        room = store.room_name(int(d.get("roomID", 0) or 0)) or ""
        action_label, action_kind = _device_action(d)
        rows.append({
            "kind": "device",
            "id": did,
            "name": str(d.get("name", "") or f"Device {did}"),
            "subtitle": f"Device · {room}" if room else "Device",
            "action_label": action_label,
            "action_kind": action_kind,
            "type": str(d.get("type", "") or ""),
            "haystack": " ".join([
                str(d.get("name", "")),
                room,
                str(d.get("type", "")),
            ]),
            "raw": d,
        })
    for s in store.all_scenes():
        if s.get("hidden"):
            continue
        try:
            sid = int(s.get("id", 0))
        except (TypeError, ValueError):
            continue
        room = store.room_name(int(s.get("roomID", 0) or 0)) or ""
        rows.append({
            "kind": "scene",
            "id": sid,
            "name": str(s.get("name", "") or f"Scene {sid}"),
            "subtitle": f"Scene · {room}" if room else "Scene",
            "action_label": "Run",
            "action_kind": "run_scene",
            "type": "",
            "haystack": " ".join([str(s.get("name", "")), room]),
            "raw": s,
        })
    return rows


class _ResultsTableSource(_NSObject):
    def initWithRows_(self, rows):  # noqa: N802
        self = objc.super(_ResultsTableSource, self).init()
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
        r = self._rows[row]
        ident = str(col.identifier())
        if ident == "name":
            return r.get("name", "")
        if ident == "subtitle":
            return r.get("subtitle", "")
        if ident == "action":
            return r.get("action_label", "")
        return ""


class SearchController:
    """Spotlight-style fuzzy search across devices and scenes.

    Wired to the existing scene/device action handlers so Enter does the
    obvious thing: run a scene, toggle a switch/dimmer, no-op otherwise.
    """

    MAX_RESULTS = 50

    def __init__(self,
                 store: StateStore,
                 on_run_scene: Callable[[int], None],
                 on_toggle_device: Callable[[int], None],
                 on_shutter_toggle: Optional[Callable[[int], None]] = None,
                 creds: Optional[HC3Credentials] = None):
        self.store = store
        self.on_run_scene = on_run_scene
        self.on_toggle_device = on_toggle_device
        self.on_shutter_toggle = on_shutter_toggle
        self.creds = creds
        self._window: Optional[NSWindow] = None
        self._target = None
        self._source: Optional[_ResultsTableSource] = None
        self._fields: dict[str, object] = {}
        self._index: list[dict] = []
        self._prev_activation_policy: Optional[int] = None

    # -- Public --------------------------------------------------------
    def show(self) -> None:
        if self._window is None:
            self._target = self._make_target()
            self._window = self._build_window()
        # Rebuild index every time we open, so newly added devices/scenes show up.
        self._index = _build_index(self.store)
        # Reset state.
        sf = self._fields.get("search")
        if sf is not None:
            sf.setStringValue_("")
        self._refresh_results()

        # LSUIElement → Regular so we can take focus.
        try:
            app = NSApplication.sharedApplication()
            if self._prev_activation_policy is None:
                self._prev_activation_policy = int(app.activationPolicy())
            app.setActivationPolicy_(0)
            app.activateIgnoringOtherApps_(True)
        except Exception:
            log.debug("could not raise activation policy", exc_info=True)
        self._window.makeKeyAndOrderFront_(None)
        try:
            self._window.orderFrontRegardless()
        except Exception:
            pass
        # Focus the search field so the user can just start typing.
        if sf is not None:
            self._window.makeFirstResponder_(sf)

    def close(self) -> None:
        if self._window is not None:
            self._window.close()

    # -- Building ------------------------------------------------------
    def _build_window(self) -> NSWindow:
        rect = NSMakeRect(0, 0, 560, 420)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable | NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_("HC3 Menu — Search")
        win.setReleasedWhenClosed_(False)
        win.setDelegate_(self._target)
        win.center()
        cv = win.contentView()

        # Search field.
        sf = NSSearchField.alloc().initWithFrame_(NSMakeRect(12, 376, 536, 32))
        sf.setPlaceholderString_("Search devices and scenes…")
        sf.setSendsSearchStringImmediately_(True)
        sf.setSendsWholeSearchString_(False)
        sf.setTarget_(self._target)
        sf.setAction_("queryChanged:")
        sf.setDelegate_(self._target)
        try:
            sf.setFont_(NSFont.systemFontOfSize_(14.0))
        except Exception:
            pass
        cv.addSubview_(sf)
        self._fields["search"] = sf

        # Results table.
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 56, 536, 308))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setBorderType_(2)  # NSBezelBorder
        scroll.setAutohidesScrollers_(False)

        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 536, 308))
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsMultipleSelection_(False)
        table.setRowHeight_(22.0)
        table.setTarget_(self._target)
        table.setDoubleAction_("rowDoubleClicked:")

        for ident, title, width in [
            ("name", "Name", 240),
            ("subtitle", "", 180),
            ("action", "Action", 110),
        ]:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            table.addTableColumn_(col)

        self._source = _ResultsTableSource.alloc().initWithRows_([])
        table.setDataSource_(self._source)
        scroll.setDocumentView_(table)
        cv.addSubview_(scroll)
        self._fields["table"] = table
        self._fields["scroll"] = scroll

        # Bottom action bar: hint label on left, Activate button on right.
        hint = NSTextField.alloc().initWithFrame_(NSMakeRect(12, 18, 380, 18))
        hint.setStringValue_(
            "↩ Run action · ⏫⏬ Move · ⎋ Close · Action shown per row")
        hint.setBezeled_(False)
        hint.setDrawsBackground_(False)
        hint.setEditable_(False)
        hint.setSelectable_(False)
        try:
            hint.setFont_(NSFont.systemFontOfSize_(11.0))
            hint.setTextColor_(NSColor.secondaryLabelColor())
        except Exception:
            pass
        cv.addSubview_(hint)
        self._fields["hint"] = hint

        activate_btn = NSButton.alloc().initWithFrame_(NSMakeRect(440, 12, 108, 32))
        activate_btn.setTitle_("Activate")
        try:
            activate_btn.setBezelStyle_(NSBezelStyleRounded)
        except Exception:
            pass
        activate_btn.setTarget_(self._target)
        activate_btn.setAction_("activateClicked:")
        cv.addSubview_(activate_btn)
        self._fields["activate_btn"] = activate_btn

        return win

    # -- Filtering -----------------------------------------------------
    def _current_query(self) -> str:
        sf = self._fields.get("search")
        if sf is None:
            return ""
        return str(sf.stringValue() or "").strip()

    def _refresh_results(self) -> None:
        if self._source is None:
            return
        q = self._current_query()
        if not q:
            # Empty query → show first N devices/scenes alphabetically.
            rows = sorted(self._index, key=lambda r: r["name"].lower())[:self.MAX_RESULTS]
        else:
            scored: list[tuple[int, dict]] = []
            for r in self._index:
                # Score against the name (primary) and the haystack (fallback).
                s_name = _fuzzy_score(q, r["name"])
                s_hay = _fuzzy_score(q, r["haystack"])
                if s_name is None and s_hay is None:
                    continue
                # Prefer name matches.
                score = max(
                    (s_name if s_name is not None else -10**6),
                    (s_hay if s_hay is not None else -10**6) - 50,
                )
                scored.append((score, r))
            scored.sort(key=lambda t: t[0], reverse=True)
            rows = [r for _, r in scored[:self.MAX_RESULTS]]
        self._source.setRows_(rows)
        table = self._fields.get("table")
        if table is not None:
            table.reloadData()
            if rows:
                # Auto-select first row so Enter targets it.
                from Foundation import NSIndexSet
                table.selectRowIndexes_byExtendingSelection_(
                    NSIndexSet.indexSetWithIndex_(0), False)
                table.scrollRowToVisible_(0)

    # -- Actions -------------------------------------------------------
    def _activate_row(self, row_idx: int) -> None:
        if self._source is None:
            return
        rows = self._source.rows()
        if row_idx < 0 or row_idx >= len(rows):
            return
        r = rows[row_idx]
        kind = r.get("action_kind")
        rid = int(r.get("id", 0))
        if kind == "run_scene":
            self.on_run_scene(rid)
        elif kind == "toggle":
            self.on_toggle_device(rid)
        elif kind == "shutter" and self.on_shutter_toggle is not None:
            self.on_shutter_toggle(rid)
        elif kind == "open_in_hc3":
            self._open_device_in_hc3(rid)
        # Keep the window open so the user can fire several actions in a row.
        # If they want to dismiss it, Esc / close button handle that.

    def _open_device_in_hc3(self, dev_id: int) -> None:
        if self.creds is None or not self.creds.base_url:
            log.info("search: no HC3 base_url configured; cannot open device %s", dev_id)
            return
        url = f"{self.creds.base_url.rstrip('/')}/fibaro/en/devices/{int(dev_id)}/edit"
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception:
            log.exception("search: failed to open %s", url)

    def _activate_selected(self) -> None:
        table = self._fields.get("table")
        if table is None:
            return
        idx = int(table.selectedRow())
        if idx < 0 and self._source and self._source.rows():
            idx = 0
        self._activate_row(idx)

    # -- Objective-C target -------------------------------------------
    def _make_target(controller_self):  # noqa: N805 - keep `self` for inner class
        outer = controller_self

        class _Target(NSObject):
            # NSSearchField action — called on each keystroke when
            # sendsSearchStringImmediately is YES.
            def queryChanged_(self, sender):  # noqa: N802
                outer._refresh_results()

            # NSTableView double-click.
            def rowDoubleClicked_(self, sender):  # noqa: N802
                idx = int(sender.clickedRow())
                outer._activate_row(idx)

            # Activate button.
            def activateClicked_(self, _sender):  # noqa: N802
                outer._activate_selected()

            # NSSearchFieldDelegate / NSTextFieldDelegate — handle Return,
            # Up/Down, Esc inside the search field.
            def control_textView_doCommandBySelector_(self, control, tv, sel):  # noqa: N802
                name = str(sel)
                table = outer._fields.get("table")
                if name == "insertNewline:":
                    outer._activate_selected()
                    return True
                if name == "cancelOperation:":
                    outer.close()
                    return True
                if name in ("moveDown:", "moveUp:") and table is not None:
                    n = (outer._source.numberOfRowsInTableView_(table)
                         if outer._source else 0)
                    if n == 0:
                        return True
                    cur = int(table.selectedRow())
                    if cur < 0:
                        cur = 0
                    new = cur + (1 if name == "moveDown:" else -1)
                    if new < 0:
                        new = 0
                    if new >= n:
                        new = n - 1
                    from Foundation import NSIndexSet
                    table.selectRowIndexes_byExtendingSelection_(
                        NSIndexSet.indexSetWithIndex_(new), False)
                    table.scrollRowToVisible_(new)
                    return True
                return False

            # NSWindowDelegate
            def windowWillClose_(self, _note):  # noqa: N802
                if outer._prev_activation_policy is not None:
                    try:
                        NSApplication.sharedApplication().setActivationPolicy_(
                            outer._prev_activation_policy
                        )
                    except Exception:
                        pass
                    outer._prev_activation_policy = None

        return _Target.alloc().init()
