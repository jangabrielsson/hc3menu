"""PyObjC Preferences window: connection settings + notification rules.

Kept intentionally simple: tabbed window with form fields and a NSTableView for devices.
Favorites are managed per-device via the menu (☆/★ entries inside each device submenu).
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
)
from Foundation import NSMutableArray

from .config import HC3Credentials, AppConfig, NotificationRule, save_credentials, save_config
from .hc3_client import HC3Client

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
        self._table_source: Optional[_DeviceTableSource] = None
        self._fields: dict[str, object] = {}

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
        view.addSubview_(self._label("Filter:", 20, 360, 50))
        filter_tf = NSTextField.alloc().initWithFrame_(NSMakeRect(70, 360, 300, 24))
        filter_tf.setTarget_(self._target)
        filter_tf.setAction_("filter:")
        view.addSubview_(filter_tf)
        self._fields["filter"] = filter_tf

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, 580, 330))
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

    # -- Actions (Objective-C target) ------------------------------------
    def _make_target(self):
        outer = self

        class _Target(NSObject):
            def save_(self, sender):  # noqa: N802
                outer._do_save()

            def cancel_(self, sender):  # noqa: N802
                outer.close()

            def test_(self, sender):  # noqa: N802
                outer._do_test()

            def filter_(self, sender):  # noqa: N802
                if outer._table_source is not None:
                    outer._table_source.setFilter_(sender.stringValue())
                    tbl = outer._fields.get("table")
                    if tbl is not None:
                        tbl.reloadData()

        return _Target.alloc().init()

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

        # Preserve existing rule details where possible
        existing = {int(r.device_id): r for r in self.config.notifications}
        rules = []
        for did in notify_ids:
            r = existing.get(int(did)) or NotificationRule(device_id=int(did))
            r.device_id = int(did)
            rules.append(r)
        new_cfg = AppConfig(
            favorites=list(self.config.favorites),  # managed via menu, not here
            notifications=rules,
            poll_timeout_sec=self.config.poll_timeout_sec,
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
        self._target = self._make_target()
        self._window = self._build_window()
        self._window.makeKeyAndOrderFront_(None)
        try:
            NSApp.activateIgnoringOtherApps_(True)
        except Exception:
            pass

    def close(self) -> None:
        if self._window is not None:
            self._window.close()
            self._window = None
