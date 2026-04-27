"""Notification rule matching + macOS notification dispatch."""
from __future__ import annotations

import logging
from typing import Optional

import rumps

from .config import NotificationRule
from .state import StateStore

log = logging.getLogger(__name__)


def _matches(rule: NotificationRule, change: dict) -> bool:
    if int(rule.device_id) != int(change.get("id", -1)):
        return False
    prop = change.get("property") or change.get("name")
    new_value = change.get("newValue")
    if rule.property and rule.property != prop:
        return False
    cond = (rule.condition or "any").strip()
    if cond == "any":
        return True
    if cond == "true":
        return bool(new_value) is True
    if cond == "false":
        return bool(new_value) is False
    try:
        if cond.startswith(">"):
            return float(new_value) > float(cond[1:])
        if cond.startswith("<"):
            return float(new_value) < float(cond[1:])
        if cond.startswith("=="):
            return str(new_value) == cond[2:]
    except (TypeError, ValueError):
        return False
    return False


def _format(rule: NotificationRule, change: dict, store: StateStore) -> tuple[str, str]:
    dev = store.get_device(int(change.get("id", -1))) or {}
    prop = change.get("property") or change.get("name")
    ctx = {
        "name": dev.get("name", f"Device {change.get('id')}"),
        "id": change.get("id"),
        "property": prop,
        "newValue": change.get("newValue"),
        "oldValue": change.get("oldValue"),
        "room": store.room_name(dev.get("roomID", 0)),
    }
    try:
        body = rule.message.format(**ctx)
    except Exception:
        body = f"{ctx['name']} {ctx['property']} -> {ctx['newValue']}"
    title = ctx["name"]
    return str(title), body


class Notifier:
    def __init__(self, store: StateStore, rules: Optional[list[NotificationRule]] = None,
                 *, attention_enabled: bool = True, low_battery_threshold: int = 20) -> None:
        self.store = store
        self.rules: list[NotificationRule] = rules or []
        self.attention_enabled = attention_enabled
        self.low_battery_threshold = low_battery_threshold
        # Dedupe sets — track devices we've already warned about so we don't
        # re-fire on every poll. Cleared automatically when state recovers.
        self._dead_notified: set[int] = set()
        self._low_batt_notified: set[int] = set()

    def set_rules(self, rules: list[NotificationRule]) -> None:
        self.rules = list(rules or [])

    def configure_attention(self, *, enabled: bool, low_battery_threshold: int) -> None:
        self.attention_enabled = enabled
        self.low_battery_threshold = low_battery_threshold

    def handle_change(self, change: dict) -> None:
        for rule in self.rules:
            if _matches(rule, change):
                title, body = _format(rule, change, self.store)
                try:
                    rumps.notification(title=title, subtitle="HC3", message=body)
                except Exception:
                    log.exception("Failed to post notification")

    # -- Attention (battery / dead) -------------------------------------
    def handle_attention(self, change: dict) -> None:
        """Detect transitions for `dead` and `batteryLevel` properties and
        post a one-shot macOS notification per transition. Caller should
        invoke this for every DevicePropertyUpdatedEvent it sees."""
        if not self.attention_enabled:
            return
        prop = change.get("property") or change.get("name")
        if prop not in ("dead", "batteryLevel"):
            return
        try:
            dev_id = int(change.get("id", -1))
        except (TypeError, ValueError):
            return
        if dev_id < 0:
            return
        dev = self.store.get_device(dev_id) or {}
        dev_name = dev.get("name", f"Device {dev_id}")
        room = self.store.room_name((dev or {}).get("roomID", 0))
        suffix = f" ({room})" if room else ""

        new_v = change.get("newValue")
        if prop == "dead":
            is_dead = bool(new_v)
            if is_dead and dev_id not in self._dead_notified:
                self._dead_notified.add(dev_id)
                self._post("Device unreachable",
                           f"{dev_name}{suffix}",
                           "HC3 reports the device as dead.")
            elif not is_dead and dev_id in self._dead_notified:
                self._dead_notified.discard(dev_id)
                self._post("Device back online",
                           f"{dev_name}{suffix}", "")
        elif prop == "batteryLevel":
            try:
                level = float(new_v)
            except (TypeError, ValueError):
                return
            if level <= self.low_battery_threshold and dev_id not in self._low_batt_notified:
                self._low_batt_notified.add(dev_id)
                self._post("Low battery",
                           f"{dev_name}{suffix}",
                           f"Battery level is {int(level)}%.")
            elif level > self.low_battery_threshold and dev_id in self._low_batt_notified:
                # Battery replaced/charged — clear so future drops re-notify.
                self._low_batt_notified.discard(dev_id)

    def reset_attention_state(self) -> None:
        """Forget previously-notified attention states (e.g. on reconnect)."""
        self._dead_notified.clear()
        self._low_batt_notified.clear()

    def _post(self, title: str, subtitle: str, message: str) -> None:
        try:
            rumps.notification(title=title, subtitle=subtitle, message=message)
        except Exception:
            log.exception("Failed to post notification")
