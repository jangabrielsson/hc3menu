"""Global keyboard hotkey support.

Uses ``NSEvent.addGlobalMonitorForEventsMatchingMask_handler_`` to observe
key-down events from anywhere in the system and fire a callback when a
configured combination is pressed.

Caveats
-------
* macOS will silently swallow the events unless the app has been granted
  the **Accessibility** permission (``System Settings → Privacy & Security
  → Accessibility``). If the callback never fires, that is almost always
  the cause.
* Global monitors **cannot consume** events — the keystroke is still
  delivered to whatever app currently has focus. Pick a chord that is
  unlikely to clash (the default is ⌃⌥⌘H).

Public API
----------
* :data:`MOD_*` constants — Cocoa modifier flag bits (re-exported for
  convenience).
* :func:`parse_chord` — turn a human-readable string like ``"ctrl+alt+cmd+H"``
  into ``(modifiers, keycode)``.
* :func:`format_chord` — inverse of :func:`parse_chord`, for display.
* :class:`GlobalHotkey` — install / uninstall a single chord.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from AppKit import (
    NSEvent,
    NSEventMaskKeyDown,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
)

log = logging.getLogger(__name__)


MOD_CMD = int(NSEventModifierFlagCommand)
MOD_CTRL = int(NSEventModifierFlagControl)
MOD_OPT = int(NSEventModifierFlagOption)
MOD_SHIFT = int(NSEventModifierFlagShift)
_ALL_MODS = MOD_CMD | MOD_CTRL | MOD_OPT | MOD_SHIFT


# Subset of the macOS virtual-keycode table — letters + digits is enough
# for choosing a hotkey from the prefs UI.
_KEYCODE_BY_NAME: dict[str, int] = {
    "A": 0,  "S": 1,  "D": 2,  "F": 3,  "H": 4,  "G": 5,  "Z": 6,  "X": 7,
    "C": 8,  "V": 9,  "B": 11, "Q": 12, "W": 13, "E": 14, "R": 15, "Y": 16,
    "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "9": 25, "7": 26, "8": 28, "0": 29, "O": 31, "U": 32, "I": 34, "P": 35,
    "L": 37, "J": 38, "K": 40, "N": 45, "M": 46,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
    "SPACE": 49, "RETURN": 36, "TAB": 48, "ESC": 53,
}
_NAME_BY_KEYCODE: dict[int, str] = {v: k for k, v in _KEYCODE_BY_NAME.items()}

_MOD_BY_NAME: dict[str, int] = {
    "CMD": MOD_CMD, "COMMAND": MOD_CMD, "⌘": MOD_CMD,
    "CTRL": MOD_CTRL, "CONTROL": MOD_CTRL, "⌃": MOD_CTRL,
    "ALT": MOD_OPT, "OPT": MOD_OPT, "OPTION": MOD_OPT, "⌥": MOD_OPT,
    "SHIFT": MOD_SHIFT, "⇧": MOD_SHIFT,
}


def parse_chord(text: str) -> Optional[tuple[int, int]]:
    """Parse e.g. ``"ctrl+alt+cmd+H"`` → ``(modifiers, keycode)`` or ``None``."""
    if not text:
        return None
    parts = [p.strip().upper() for p in text.replace(" ", "+").split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    key: Optional[int] = None
    for p in parts:
        if p in _MOD_BY_NAME:
            mods |= _MOD_BY_NAME[p]
        elif p in _KEYCODE_BY_NAME:
            if key is not None:
                return None  # two non-modifier keys
            key = _KEYCODE_BY_NAME[p]
        else:
            return None
    if key is None:
        return None
    return (mods, key)


def format_chord(mods: int, keycode: int) -> str:
    """Render ``(modifiers, keycode)`` back to a display string with glyphs."""
    bits: list[str] = []
    if mods & MOD_CTRL:  bits.append("⌃")
    if mods & MOD_OPT:   bits.append("⌥")
    if mods & MOD_SHIFT: bits.append("⇧")
    if mods & MOD_CMD:   bits.append("⌘")
    bits.append(_NAME_BY_KEYCODE.get(int(keycode), f"Key{keycode}"))
    return "".join(bits)


class GlobalHotkey:
    """Install one global key-down monitor for a single (mods, keycode) chord.

    Call :meth:`install` to start, :meth:`uninstall` to stop, and
    :meth:`replace` to swap the chord while running.
    """

    def __init__(self, on_fire: Callable[[], None]):
        self._on_fire = on_fire
        self._monitor = None  # opaque object returned by AppKit
        self._mods: int = 0
        self._keycode: int = 0

    @property
    def is_installed(self) -> bool:
        return self._monitor is not None

    def install(self, mods: int, keycode: int) -> bool:
        """Start listening. Returns True on success."""
        if self._monitor is not None:
            self.uninstall()
        self._mods = int(mods) & _ALL_MODS
        self._keycode = int(keycode)
        if self._mods == 0:
            log.warning("global_hotkey: refusing to register chord with no modifiers (keycode=%s)",
                        self._keycode)
            return False

        target_mods = self._mods
        target_key = self._keycode
        on_fire = self._on_fire

        def _handler(event):  # noqa: ANN001 — Cocoa NSEvent
            try:
                if int(event.keyCode()) != target_key:
                    return
                # Mask off device-specific bits; only check the four core mods.
                if (int(event.modifierFlags()) & _ALL_MODS) != target_mods:
                    return
                on_fire()
            except Exception:
                log.exception("global_hotkey: handler raised")

        try:
            self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, _handler
            )
        except Exception:
            log.exception("global_hotkey: failed to install monitor")
            self._monitor = None
            return False
        if self._monitor is None:
            log.warning("global_hotkey: addGlobalMonitor returned nil "
                        "(missing Accessibility permission?)")
            return False
        log.info("global_hotkey: installed for %s", format_chord(self._mods, self._keycode))
        return True

    def uninstall(self) -> None:
        if self._monitor is None:
            return
        try:
            NSEvent.removeMonitor_(self._monitor)
        except Exception:
            log.exception("global_hotkey: removeMonitor failed")
        self._monitor = None
        log.debug("global_hotkey: uninstalled")

    def replace(self, mods: int, keycode: int) -> bool:
        """Atomically swap to a new chord. Returns True on success."""
        was_installed = self._monitor is not None
        self.uninstall()
        if not was_installed:
            self._mods, self._keycode = int(mods) & _ALL_MODS, int(keycode)
            return True
        return self.install(mods, keycode)
