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
    def __init__(self, store: StateStore, rules: Optional[list[NotificationRule]] = None) -> None:
        self.store = store
        self.rules: list[NotificationRule] = rules or []

    def set_rules(self, rules: list[NotificationRule]) -> None:
        self.rules = list(rules or [])

    def handle_change(self, change: dict) -> None:
        for rule in self.rules:
            if _matches(rule, change):
                title, body = _format(rule, change, self.store)
                try:
                    rumps.notification(title=title, subtitle="HC3", message=body)
                except Exception:
                    log.exception("Failed to post notification")
