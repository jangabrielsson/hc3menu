"""PyObjC Preferences window: connection settings + notification rules.

Kept intentionally simple: tabbed window with form fields and a NSTableView for devices.
Favorites are starred via each device submenu in the menu bar; their order is
managed in the Favorites tab here (drag to reorder, − to remove).
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import objc
from AppKit import (
    NSApp, NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSBackingStoreBuffered,
    NSTabView, NSTabViewItem, NSTextField, NSSecureTextField, NSButton,
    NSButtonTypeSwitch, NSButtonTypeMomentaryPushIn, NSBezelStyleRounded,
    NSScrollView, NSTableView, NSTableColumn, NSAlert, NSObject,
    NSMakeRect, NSMakeSize, NSWindowController,
    NSEvent, NSEventMaskKeyDown,
    NSPasteboard, NSPasteboardItem,
    NSTableViewDropAbove, NSDragOperationMove,
)
from Foundation import NSMutableArray

from .config import HC3Credentials, AppConfig, NotificationRule, save_credentials, save_config
from .hc3_client import HC3Client
from . import global_hotkey as gh_mod

log = logging.getLogger(__name__)


class _DeviceTableSource(NSObject):
    """NSTableView data source for the notifications table."""

    def initWithDevices_notifyIds_(self, devices, notify_ids):  # noqa: N802
        self = objc.super(_DeviceTableSource, self).init()
        if self is None:
            return None
        self._devices = list(devices)
        self._notify = set(int(x) for x in notify_ids)
        self._filter = ""
        return self

    def setFilter_(self, text):  # noqa: N802
        self._filter = (text or "").lower()

    def filteredDevices(self):  # noqa: N802
        if not self._filter:
            return self._devices
        f = self._filter
        return [d for d in self._devices
                if f in str(d.get("name", "")).lower()
                or f in str(d.get("type", "")).lower()]

    def numberOfRowsInTableView_(self, tv):  # noqa: N802
        return len(self.filteredDevices())

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):  # noqa: N802
        dev = self.filteredDevices()[row]
        ident = str(col.identifier())
        if ident == "notify":
            return 1 if int(dev["id"]) in self._notify else 0
        if ident == "id":
            return str(dev.get("id"))
        if ident == "name":
            return str(dev.get("name", ""))
        if ident == "type":
            return str(dev.get("type", ""))
        return ""

    def tableView_setObjectValue_forTableColumn_row_(self, tv, value, col, row):  # noqa: N802
        dev = self.filteredDevices()[row]
        dev_id = int(dev["id"])
        ident = str(col.identifier())
        on = bool(value)
        if ident != "notify":
            return
        if on:
            self._notify.add(dev_id)
        else:
            self._notify.discard(dev_id)

    # Accessors used by the controller on save
    def notifyIds(self):  # noqa: N802
        return sorted(self._notify)


# Pasteboard UTI used to identify a favorites-row drag.
_FAV_PB_TYPE = "com.jangabrielsson.hc3menu.favrow"


class _FavoritesTableSource(NSObject):
    """NSTableView data source for the favorites reorder table."""

    def initWithDevices_favorites_(self, devices, favorites):  # noqa: N802
        self = objc.super(_FavoritesTableSource, self).init()
        if self is None:
            return None
        self._by_id = {int(d["id"]): d for d in devices}
        # Filter favorites down to those still present, preserving order.
        self._fav_ids: list[int] = [int(f) for f in favorites if int(f) in self._by_id]
        return self

    def favoriteIds(self):  # noqa: N802
        return list(self._fav_ids)

    def removeRow_(self, row: int) -> None:  # noqa: N802
        if 0 <= int(row) < len(self._fav_ids):
            del self._fav_ids[int(row)]

    def numberOfRowsInTableView_(self, tv):  # noqa: N802
        return len(self._fav_ids)

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):  # noqa: N802
        try:
            dev = self._by_id[self._fav_ids[row]]
        except (IndexError, KeyError):
            return ""
        ident = str(col.identifier())
        if ident == "id":
            return str(dev.get("id"))
        if ident == "name":
            return str(dev.get("name", ""))
        if ident == "type":
            return str(dev.get("type", ""))
        if ident == "room":
            return str(dev.get("roomName", "") or "")
        return ""

    # ---- Drag & drop reorder ----------------------------------------
    def tableView_pasteboardWriterForRow_(self, tv, row):  # noqa: N802
        item = NSPasteboardItem.alloc().init()
        item.setString_forType_(str(int(row)), _FAV_PB_TYPE)
        return item

    def tableView_validateDrop_proposedRow_proposedDropOperation_(  # noqa: N802
        self, tv, info, row, op
    ):
        if int(op) != int(NSTableViewDropAbove):
            return 0  # NSDragOperationNone
        return int(NSDragOperationMove)

    def tableView_acceptDrop_row_dropOperation_(self, tv, info, row, op):  # noqa: N802
        pb = info.draggingPasteboard()
        s = pb.stringForType_(_FAV_PB_TYPE)
        if s is None:
            return False
        try:
            src = int(s)
        except ValueError:
            return False
        dst = int(row)
        if not (0 <= src < len(self._fav_ids)):
            return False
        # NSTableView delivers the destination row index assuming the source
        # is still in place; adjust if removing it shifts the target down.
        moved = self._fav_ids.pop(src)
        if dst > src:
            dst -= 1
        dst = max(0, min(dst, len(self._fav_ids)))
        self._fav_ids.insert(dst, moved)
        tv.reloadData()
        # Re-select the moved row for visual continuity.
        from Foundation import NSIndexSet
        tv.selectRowIndexes_byExtendingSelection_(
            NSIndexSet.indexSetWithIndex_(dst), False
        )
        return True


class _PrefsTarget(NSObject):
    """Module-level Objective-C target for the prefs window.

    Defined once at import time so we don't re-register the class on
    every ``show()``, which Objective-C forbids.
    """

    def initWithOuter_(self, outer):  # noqa: N802
        self = objc.super(_PrefsTarget, self).init()
        if self is None:
            return None
        self._outer = outer
        return self

    def save_(self, _sender):  # noqa: N802
        self._outer._do_save()

    def cancel_(self, _sender):  # noqa: N802
        self._outer.close()

    def test_(self, _sender):  # noqa: N802
        self._outer._do_test()

    def httpsToggled_(self, sender):  # noqa: N802
        outer = self._outer
        port_tf = outer._fields.get("port")
        if port_tf is None:
            return
        https_on = bool(sender.state())
        try:
            current_port = int(port_tf.stringValue().strip())
        except (ValueError, AttributeError):
            current_port = None
        # Auto-switch between default HTTP (80) and HTTPS (443) ports only
        # when the user hasn't entered a custom port.
        if https_on and current_port == 80:
            port_tf.setStringValue_("443")
        elif not https_on and current_port == 443:
            port_tf.setStringValue_("80")

    def filter_(self, sender):  # noqa: N802
        outer = self._outer
        if outer._table_source is not None:
            outer._table_source.setFilter_(sender.stringValue())
            tbl = outer._fields.get("table")
            if tbl is not None:
                tbl.reloadData()

    def recordHotkey_(self, _sender):  # noqa: N802
        self._outer._start_recording_hotkey()

    def resetHotkey_(self, _sender):  # noqa: N802
        self._outer._reset_hotkey_to_default()

    def removeFavorite_(self, _sender):  # noqa: N802
        outer = self._outer
        tbl = outer._fields.get("fav_table")
        src = outer._fav_source
        if tbl is None or src is None:
            return
        row = int(tbl.selectedRow())
        if row < 0:
            return
        src.removeRow_(row)
        tbl.reloadData()

    def windowWillClose_(self, _note):  # noqa: N802
        self._outer._on_window_will_close()


class PreferencesController:
    """Thin Python wrapper that builds and shows the prefs window."""

    def __init__(self, creds: HC3Credentials, config: AppConfig,
                 devices: list[dict],
                 on_save: Callable[[HC3Credentials, AppConfig], None]):
        self.creds = creds
        self.config = config
        self.devices = devices
        self.on_save = on_save
        self._window: Optional[NSWindow] = None
        self._target = None  # _PrefsTarget; created lazily
        self._table_source: Optional[_DeviceTableSource] = None
        self._fav_source: Optional[_FavoritesTableSource] = None
        self._fields: dict[str, object] = {}
        self._prev_activation_policy: Optional[int] = None

    # -- Layout helpers ---------------------------------------------------
    def _build_window(self) -> NSWindow:
        rect = NSMakeRect(200, 200, 640, 480)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskResizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False
        )
        win.setTitle_("HC3 Menu — Preferences")
        win.setReleasedWhenClosed_(False)

        tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(10, 50, 620, 420))
        win.contentView().addSubview_(tabs)

        tabs.addTabViewItem_(self._build_connection_tab())
        tabs.addTabViewItem_(self._build_notifications_tab())
        tabs.addTabViewItem_(self._build_favorites_tab())
        tabs.addTabViewItem_(self._build_shortcuts_tab())

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(540, 10, 90, 30))
        save_btn.setTitle_("Save")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setButtonType_(NSButtonTypeMomentaryPushIn)
        save_btn.setTarget_(self._target)
        save_btn.setAction_("save:")
        win.contentView().addSubview_(save_btn)

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(440, 10, 90, 30))
        cancel_btn.setTitle_("Close")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(self._target)
        cancel_btn.setAction_("cancel:")
        win.contentView().addSubview_(cancel_btn)

        return win

    def _label(self, text: str, x: float, y: float, w: float = 120) -> NSTextField:
        lbl = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 22))
        lbl.setStringValue_(text)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        return lbl

    def _build_connection_tab(self) -> NSTabViewItem:
        item = NSTabViewItem.alloc().initWithIdentifier_("conn")
        item.setLabel_("Connection")
        view = item.view()

        rows = [
            ("Host", "host", self.creds.host, NSTextField),
            ("Port", "port", str(self.creds.port), NSTextField),
            ("User", "user", self.creds.user, NSTextField),
            ("Password", "password", self.creds.password, NSSecureTextField),
            ("Alarm PIN", "pin", self.creds.pin, NSSecureTextField),
        ]
        y = 340
        for label, key, value, cls in rows:
            view.addSubview_(self._label(label, 20, y))
            tf = cls.alloc().initWithFrame_(NSMakeRect(150, y, 360, 24))
            tf.setStringValue_(value or "")
            view.addSubview_(tf)
            self._fields[key] = tf
            y -= 36

        view.addSubview_(self._label("HTTPS", 20, y))
        https_btn = NSButton.alloc().initWithFrame_(NSMakeRect(150, y, 80, 22))
        https_btn.setButtonType_(NSButtonTypeSwitch)
        https_btn.setTitle_("Enable")
        https_btn.setState_(1 if self.creds.https else 0)
        https_btn.setTarget_(self._target)
        https_btn.setAction_("httpsToggled:")
        view.addSubview_(https_btn)
        self._fields["https"] = https_btn

        test_btn = NSButton.alloc().initWithFrame_(NSMakeRect(150, y - 50, 160, 30))
        test_btn.setTitle_("Test connection")
        test_btn.setBezelStyle_(NSBezelStyleRounded)
        test_btn.setTarget_(self._target)
        test_btn.setAction_("test:")
        view.addSubview_(test_btn)
        return item

    def _build_notifications_tab(self) -> NSTabViewItem:
        item = NSTabViewItem.alloc().initWithIdentifier_("notify")
        item.setLabel_("Notifications")
        view = item.view()

        notify_ids = [r.device_id for r in self.config.notifications]
        self._table_source = _DeviceTableSource.alloc().initWithDevices_notifyIds_(
            self.devices, notify_ids
        )

        # Filter field
        view.addSubview_(self._label("Filter:", 20, 340, 50))
        filter_tf = NSTextField.alloc().initWithFrame_(NSMakeRect(70, 340, 300, 24))
        filter_tf.setTarget_(self._target)
        filter_tf.setAction_("filter:")
        view.addSubview_(filter_tf)
        self._fields["filter"] = filter_tf

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, 580, 310))
        scroll.setHasVerticalScroller_(True)
        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 580, 330))

        for ident, title, width, editable in [
            ("notify", "Notify", 60, True),
            ("id", "ID", 50, False),
            ("name", "Name", 280, False),
            ("type", "Type", 180, False),
        ]:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            col.setEditable_(editable)
            if ident == "notify":
                from AppKit import NSButtonCell
                cell = NSButtonCell.alloc().init()
                cell.setButtonType_(NSButtonTypeSwitch)
                cell.setTitle_("")
                col.setDataCell_(cell)
            table.addTableColumn_(col)

        table.setDataSource_(self._table_source)
        scroll.setDocumentView_(table)
        view.addSubview_(scroll)
        self._fields["table"] = table
        return item

    def _build_favorites_tab(self) -> NSTabViewItem:
        item = NSTabViewItem.alloc().initWithIdentifier_("favs")
        item.setLabel_("Favorites")
        view = item.view()

        # Build fresh source each time the window is constructed.
        self._fav_source = _FavoritesTableSource.alloc().initWithDevices_favorites_(
            self.devices, self.config.favorites
        )

        view.addSubview_(self._label(
            "Drag rows to reorder. Order is reflected in the menu bar.",
            20, 340, 560,
        ))

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 60, 580, 270))
        scroll.setHasVerticalScroller_(True)
        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 580, 290))

        for ident, title, width in [
            ("id",   "ID",    50),
            ("name", "Name",  260),
            ("type", "Type",  150),
            ("room", "Room",  110),
        ]:
            col = NSTableColumn.alloc().initWithIdentifier_(ident)
            col.headerCell().setStringValue_(title)
            col.setWidth_(width)
            col.setEditable_(False)
            table.addTableColumn_(col)

        table.setDataSource_(self._fav_source)
        # Enable intra-table drag reorder.
        table.registerForDraggedTypes_([_FAV_PB_TYPE])
        # NSDragOperationMove == 16
        table.setDraggingSourceOperationMask_forLocal_(16, True)
        scroll.setDocumentView_(table)
        view.addSubview_(scroll)
        self._fields["fav_table"] = table

        # Remove button.
        remove_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, 20, 110, 28))
        remove_btn.setTitle_("Remove")
        remove_btn.setBezelStyle_(NSBezelStyleRounded)
        remove_btn.setTarget_(self._target)
        remove_btn.setAction_("removeFavorite:")
        view.addSubview_(remove_btn)

        return item

    def _build_shortcuts_tab(self) -> NSTabViewItem:
        item = NSTabViewItem.alloc().initWithIdentifier_("shortcuts")
        item.setLabel_("Shortcuts")
        view = item.view()

        # Enabled toggle.
        enabled_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, 340, 320, 22))
        enabled_btn.setButtonType_(NSButtonTypeSwitch)
        enabled_btn.setTitle_("Enable global hotkey to open the menu")
        enabled_btn.setState_(1 if self.config.global_hotkey_enabled else 0)
        view.addSubview_(enabled_btn)
        self._fields["hotkey_enabled"] = enabled_btn

        # Current chord display.
        view.addSubview_(self._label("Shortcut", 20, 320))
        chord_tf = NSTextField.alloc().initWithFrame_(NSMakeRect(150, 320, 200, 24))
        chord_tf.setStringValue_(self._format_chord_or_default())
        chord_tf.setBezeled_(True)
        chord_tf.setDrawsBackground_(True)
        chord_tf.setEditable_(False)
        chord_tf.setSelectable_(True)
        view.addSubview_(chord_tf)
        self._fields["hotkey_chord"] = chord_tf

        # Record button toggles between "Record…" and "Press a chord — Esc to cancel".
        record_btn = NSButton.alloc().initWithFrame_(NSMakeRect(360, 318, 130, 28))
        record_btn.setTitle_("Record…")
        record_btn.setBezelStyle_(NSBezelStyleRounded)
        record_btn.setTarget_(self._target)
        record_btn.setAction_("recordHotkey:")
        view.addSubview_(record_btn)
        self._fields["hotkey_record_btn"] = record_btn

        # Reset button.
        reset_btn = NSButton.alloc().initWithFrame_(NSMakeRect(500, 318, 100, 28))
        reset_btn.setTitle_("Reset")
        reset_btn.setBezelStyle_(NSBezelStyleRounded)
        reset_btn.setTarget_(self._target)
        reset_btn.setAction_("resetHotkey:")
        view.addSubview_(reset_btn)

        # Help text.
        help_y = 250
        for line in (
            "Press the new chord while in record mode. Modifiers (⌘ ⌃ ⌥ ⇧)",
            "are required; pure letter keys cannot be used.",
            "",
            "macOS requires Accessibility permission for global hotkeys:",
            "System Settings → Privacy & Security → Accessibility → enable HC3 Menu.",
            "",
            "Note: the chord is observed only — the keystroke is still delivered to",
            "whichever app currently has focus, so pick a combo unlikely to clash.",
        ):
            lbl = self._label(line, 20, help_y, 580)
            view.addSubview_(lbl)
            help_y -= 22

        return item

    def _format_chord_or_default(self) -> str:
        chord = gh_mod.parse_chord(self.config.global_hotkey or "")
        if chord is None:
            return "(none)"
        m, k = chord
        return gh_mod.format_chord(m, k)

    # -- Hotkey recorder ------------------------------------------------
    def _start_recording_hotkey(self) -> None:
        if getattr(self, "_hotkey_monitor", None) is not None:
            return  # already recording
        btn = self._fields.get("hotkey_record_btn")
        if btn is not None:
            btn.setTitle_("Press chord — Esc to cancel")

        def handler(event):  # noqa: ANN001 — NSEvent
            try:
                key = int(event.keyCode())
                mods = int(event.modifierFlags()) & gh_mod._ALL_MODS
                # Esc cancels.
                if key == 53 and mods == 0:
                    self._stop_recording_hotkey(commit=False)
                    return None  # consume
                if mods == 0:
                    # Ignore bare keys; require at least one modifier.
                    return None
                self._stop_recording_hotkey(commit=True, mods=mods, key=key)
                return None  # consume
            except Exception:
                log.exception("hotkey recorder failed")
                self._stop_recording_hotkey(commit=False)
                return event

        try:
            self._hotkey_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, handler
            )
        except Exception:
            log.exception("hotkey recorder: addLocalMonitor failed")
            self._hotkey_monitor = None
            if btn is not None:
                btn.setTitle_("Record…")

    def _stop_recording_hotkey(self, commit: bool,
                               mods: int = 0, key: int = 0) -> None:
        mon = getattr(self, "_hotkey_monitor", None)
        if mon is not None:
            try:
                NSEvent.removeMonitor_(mon)
            except Exception:
                log.exception("hotkey recorder: removeMonitor failed")
            self._hotkey_monitor = None
        btn = self._fields.get("hotkey_record_btn")
        if btn is not None:
            btn.setTitle_("Record…")
        if commit:
            chord_text = self._chord_to_config_string(mods, key)
            self.config.global_hotkey = chord_text
            tf = self._fields.get("hotkey_chord")
            if tf is not None:
                tf.setStringValue_(gh_mod.format_chord(mods, key))

    @staticmethod
    def _chord_to_config_string(mods: int, key: int) -> str:
        """Convert (mods, keycode) → human-parseable string for AppConfig."""
        bits: list[str] = []
        if mods & gh_mod.MOD_CTRL:  bits.append("ctrl")
        if mods & gh_mod.MOD_OPT:   bits.append("alt")
        if mods & gh_mod.MOD_SHIFT: bits.append("shift")
        if mods & gh_mod.MOD_CMD:   bits.append("cmd")
        bits.append(gh_mod._NAME_BY_KEYCODE.get(int(key), f"Key{key}"))
        return "+".join(bits)

    def _reset_hotkey_to_default(self) -> None:
        self.config.global_hotkey = "ctrl+alt+cmd+H"
        tf = self._fields.get("hotkey_chord")
        if tf is not None:
            tf.setStringValue_(self._format_chord_or_default())

    # -- Actions (Objective-C target) ------------------------------------
    def _make_target(self):
        return _PrefsTarget.alloc().initWithOuter_(self)

    def _read_creds(self) -> HC3Credentials:
        try:
            port = int(self._fields["port"].stringValue() or "80")
        except ValueError:
            port = 80
        return HC3Credentials(
            host=str(self._fields["host"].stringValue()).strip(),
            port=port,
            https=bool(self._fields["https"].state()),
            user=str(self._fields["user"].stringValue()).strip(),
            password=str(self._fields["password"].stringValue()),
            pin=str(self._fields["pin"].stringValue()).strip(),
        )

    def _do_test(self) -> None:
        creds = self._read_creds()
        ok, msg = HC3Client(creds, request_timeout=5).test_connection()
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Connection OK" if ok else "Connection failed")
        alert.setInformativeText_(msg)
        alert.runModal()

    def _do_save(self) -> None:
        creds = self._read_creds()
        notify_ids = self._table_source.notifyIds() if self._table_source else []

        # Hotkey enabled comes from the checkbox; the chord text is updated
        # live by the recorder into self.config.global_hotkey.
        hk_btn = self._fields.get("hotkey_enabled")
        hk_enabled = bool(hk_btn.state()) if hk_btn is not None else self.config.global_hotkey_enabled

        # Preserve existing rule details where possible
        existing = {int(r.device_id): r for r in self.config.notifications}
        rules = []
        for did in notify_ids:
            r = existing.get(int(did)) or NotificationRule(device_id=int(did))
            r.device_id = int(did)
            rules.append(r)
        new_cfg = AppConfig(
            favorites=(self._fav_source.favoriteIds()
                       if self._fav_source is not None
                       else list(self.config.favorites)),
            notifications=rules,
            poll_timeout_sec=self.config.poll_timeout_sec,
            attention_notifications=self.config.attention_notifications,
            low_battery_threshold=self.config.low_battery_threshold,
            qa_error_notifications=self.config.qa_error_notifications,
            qa_error_throttle_sec=self.config.qa_error_throttle_sec,
            qa_crash_notifications=self.config.qa_crash_notifications,
            auto_update_check=self.config.auto_update_check,
            auto_update_interval_sec=self.config.auto_update_interval_sec,
            auto_update_last_check=self.config.auto_update_last_check,
            global_hotkey_enabled=hk_enabled,
            global_hotkey=self.config.global_hotkey,
        )
        save_credentials(creds)
        save_config(new_cfg)
        self.creds = creds
        self.config = new_cfg
        try:
            self.on_save(creds, new_cfg)
        except Exception:
            log.exception("on_save callback failed")
        self.close()

    def show(self) -> None:
        if self._window is None:
            self._target = self._make_target()
            self._window = self._build_window()
            try:
                self._window.setDelegate_(self._target)
            except Exception:
                pass
        # LSUIElement → Regular so the window can take focus.
        try:
            from AppKit import NSApplication
            app = NSApplication.sharedApplication()
            if self._prev_activation_policy is None:
                self._prev_activation_policy = int(app.activationPolicy())
            app.setActivationPolicy_(0)
            app.activateIgnoringOtherApps_(True)
        except Exception:
            log.debug("prefs: could not raise activation policy", exc_info=True)
        self._window.makeKeyAndOrderFront_(None)
        try:
            self._window.orderFrontRegardless()
        except Exception:
            pass

    def close(self) -> None:
        if self._window is not None:
            self._window.close()

    def _on_window_will_close(self) -> None:
        # Drop the window so the next show() rebuilds with fresh data, but
        # keep the _Target instance — re-creating an Objective-C subclass
        # is what triggered the "overriding existing Objective-C class"
        # error before.
        self._window = None
        if self._prev_activation_policy is not None:
            try:
                from AppKit import NSApplication
                NSApplication.sharedApplication().setActivationPolicy_(
                    self._prev_activation_policy
                )
            except Exception:
                pass
            self._prev_activation_policy = None
