"""Build rumps menu items from HC3 device records."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Optional

import rumps

from .state import StateStore
from .sf_symbols import sf_image

try:
    from AppKit import NSColor
    _HAS_APPKIT = True
except ImportError:  # pragma: no cover
    _HAS_APPKIT = False

log = logging.getLogger(__name__)


def _set_icon(item: rumps.MenuItem, name: str, color=None) -> None:
    """Attach an SF Symbol to a rumps MenuItem (silent no-op on failure)."""
    try:
        img = sf_image(name, color=color)
        if img is not None:
            item._menuitem.setImage_(img)
    except Exception:
        pass


def _make_favorite_item(dev_id: int, is_fav: bool,
                        on_toggle: Optional[Callable[[int], None]]
                        ) -> Optional[rumps.MenuItem]:
    """Build the per-device 'Add/Remove favorite' submenu entry.

    Returns None when no toggle callback is wired (e.g. unit tests).
    """
    if on_toggle is None:
        return None
    label = "★ Remove from favorites" if is_fav else "☆ Add to favorites"
    item = rumps.MenuItem(label)
    if _HAS_APPKIT:
        _set_icon(
            item,
            "star.fill" if is_fav else "star",
            color=NSColor.systemYellowColor() if is_fav else NSColor.secondaryLabelColor(),
        )
    item.set_callback(lambda _i: on_toggle(dev_id))
    return item


def _build_slider_item(initial: int, on_change: Callable[[int], None],
                       *, min_v: int = 0, max_v: int = 100,
                       unit: str = "%", step: int = 5,
                       label_width: float = 56.0) -> rumps.MenuItem:
    """Return a rumps.MenuItem whose underlying NSMenuItem has a slider view."""
    from .slider_view import make_slider_view
    item = rumps.MenuItem("")  # title hidden by view
    view = make_slider_view(initial, on_change,
                            min_v=min_v, max_v=max_v,
                            unit=unit, step=step, label_width=label_width)
    # rumps wraps the AppKit NSMenuItem at item._menuitem
    item._menuitem.setView_(view)
    return item

# Device-type classification --------------------------------------------------
SWITCH_TYPES = {
    "com.fibaro.binarySwitch",
    "com.fibaro.developer.bxt.binarySwitch",
    "com.fibaro.FGWP101", "com.fibaro.FGWP102",
}
DIMMER_TYPES = {
    "com.fibaro.multilevelSwitch",
    "com.fibaro.FGD212",
}
SHUTTER_TYPES = {
    "com.fibaro.FGRM222",
    "com.fibaro.rollerShutter",
    "com.fibaro.baseShutter",
}
TEMP_SENSOR_TYPES = {"com.fibaro.temperatureSensor"}
LUX_SENSOR_TYPES = {"com.fibaro.lightSensor"}
HUMIDITY_TYPES = {"com.fibaro.humiditySensor"}
MOTION_TYPES = {"com.fibaro.motionSensor"}
THERMOSTAT_TYPES = {
    "com.fibaro.hvacSystem",
    "com.fibaro.thermostatDanfoss",
    "com.fibaro.thermostatHorstmann",
    "com.fibaro.setPoint",
}


def classify(device: dict) -> Optional[str]:
    t = device.get("type", "")
    base = device.get("baseType", "")
    if t in SWITCH_TYPES or base in SWITCH_TYPES:
        return "switch"
    if t in DIMMER_TYPES or base in DIMMER_TYPES:
        return "dimmer"
    if t in SHUTTER_TYPES or base in SHUTTER_TYPES:
        return "shutter"
    if t in TEMP_SENSOR_TYPES or base in TEMP_SENSOR_TYPES:
        return "temp_sensor"
    if t in LUX_SENSOR_TYPES or base in LUX_SENSOR_TYPES:
        return "lux_sensor"
    if t in HUMIDITY_TYPES or base in HUMIDITY_TYPES:
        return "humidity_sensor"
    if t in MOTION_TYPES or base in MOTION_TYPES:
        return "motion_sensor"
    if t in THERMOSTAT_TYPES or base in THERMOSTAT_TYPES:
        return "thermostat"
    return None


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    if isinstance(v, str):
        return v.lower() in ("true", "1", "on", "yes")
    return False


# -- Menu item factories ------------------------------------------------------

class MenuActionDispatcher:
    """Holds the callable that runs a device action off the UI thread."""

    def __init__(self, run_async: Callable[[Callable[[], None]], None]):
        self._run_async = run_async

    def submit(self, fn: Callable[[], None]) -> None:
        self._run_async(fn)


# -- Status glyphs ------------------------------------------------------------
# All glyphs use color emoji so every row has uniform width in the menu.
GLYPH_OFF = "⚫"      # switch / dimmer / light off
GLYPH_ON = "🟡"       # switch / plain dimmer on
GLYPH_SHUTTER = "🪟"
GLYPH_TEMP = "🌡️"
GLYPH_LUX = "☀️"
GLYPH_HUMIDITY = "💧"
GLYPH_MOTION_ACTIVE = "🟢"
GLYPH_MOTION_IDLE = "⚪"
GLYPH_THERMOSTAT = "🔥"
GLYPH_UNKNOWN = "▫️"
GLYPH_ALARM_ARMED = "🔴"
GLYPH_ALARM_DISARMED = "🟢"
GLYPH_ALARM_BREACHED = "🚨"
GLYPH_ALARM_PENDING = "⏳"
GLYPH_PROFILE = "🎭"
GLYPH_PROFILE_ACTIVE = "✅"
GLYPH_DIAG = "📊"
GLYPH_CPU = "🧠"
GLYPH_RAM = "💾"
GLYPH_DISK = "💽"

# Color controller types (RGB / RGBW / CCT lights).
COLOR_CONTROLLER_TYPES = {"com.fibaro.colorController"}


def _is_color_controller(device: dict) -> bool:
    return (device.get("type") in COLOR_CONTROLLER_TYPES
            or device.get("baseType") in COLOR_CONTROLLER_TYPES)


def _rgb_to_hue(r: int, g: int, b: int) -> Optional[float]:
    """Return hue in degrees [0,360) or None if achromatic."""
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx == mn:
        return None
    d = mx - mn
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = ((b - r) / d) + 2
    else:
        h = ((r - g) / d) + 4
    return (h * 60) % 360


def _color_glyph(props: dict) -> str:
    """Pick a colored emoji circle reflecting the device's current color."""
    cc = props.get("colorComponents") or {}
    if not isinstance(cc, dict):
        cc = {}
    r = int(cc.get("red", 0) or 0)
    g = int(cc.get("green", 0) or 0)
    b = int(cc.get("blue", 0) or 0)
    ww = int(cc.get("warmWhite", 0) or 0)
    cw = int(cc.get("coldWhite", cc.get("white", 0)) or 0)
    amber = int(cc.get("amber", 0) or 0)
    cyan = int(cc.get("cyan", 0) or 0)
    purple = int(cc.get("purple", 0) or 0)

    rgb_max = max(r, g, b)
    white_max = max(ww, cw)
    extra_max = max(amber, cyan, purple)

    # No chroma at all: fall back to dim-level glyph
    if rgb_max < 16 and white_max < 16 and extra_max < 16:
        try:
            v = int(props.get("value", 0) or 0)
        except (TypeError, ValueError):
            v = 0
        return _level_glyph(v)

    # Extra single-channel dominates over r/g/b
    if extra_max >= rgb_max and extra_max >= 16:
        if amber == extra_max:
            return "🟠"
        if cyan == extra_max:
            return "🔵"
        if purple == extra_max:
            return "🟣"

    if rgb_max < 16:
        # Pure white channels
        return "🟤" if ww > cw else "⚪"

    hue = _rgb_to_hue(r, g, b)
    if hue is None:
        return "⚪"
    if hue < 15 or hue >= 345:
        return "🔴"
    if hue < 45:
        return "🟠"
    if hue < 70:
        return "🟡"
    if hue < 170:
        return "🟢"
    if hue < 260:
        return "🔵"
    if hue < 345:
        return "🟣"
    return "⚪"


def _level_glyph(level) -> str:
    """Plain dimmers always show the same on glyph; level is shown numerically."""
    try:
        n = int(level)
    except (TypeError, ValueError):
        return GLYPH_OFF
    return GLYPH_OFF if n <= 0 else GLYPH_ON


def build_switch_item(device: dict, store: StateStore,
                      dispatcher: MenuActionDispatcher,
                      client,
                      is_fav: bool = False,
                      on_favorite_toggle: Optional[Callable[[int], None]] = None
                      ) -> rumps.MenuItem:
    name = device.get("name", f"Device {device['id']}")
    props = device.get("properties", {}) or {}
    is_on = _truthy(props.get("value"))
    value_text = "On" if is_on else "Off"
    dev_id = int(device["id"])

    # Parent shows status; primary toggle lives as first submenu item so we
    # can also expose the favorite toggle.
    parent = rumps.MenuItem(f"{name} — {value_text}")
    _set_icon(parent, "power.circle.fill" if is_on else "power",
              color=NSColor.systemGreenColor() if is_on else NSColor.secondaryLabelColor())
    state_holder = {"on": is_on}

    toggle = rumps.MenuItem("Turn off" if is_on else "Turn on")
    _set_icon(toggle, "power",
              color=NSColor.systemRedColor() if is_on else NSColor.systemGreenColor())

    def cb(_):
        new_state = not state_holder["on"]
        state_holder["on"] = new_state
        parent.title = f"{name} — {'On' if new_state else 'Off'}"
        _set_icon(parent, "power.circle.fill" if new_state else "power",
                  color=NSColor.systemGreenColor() if new_state else NSColor.secondaryLabelColor())
        toggle.title = "Turn off" if new_state else "Turn on"
        if new_state:
            dispatcher.submit(lambda: client.turn_on(dev_id))
        else:
            dispatcher.submit(lambda: client.turn_off(dev_id))

    toggle.set_callback(cb)
    parent.add(toggle)

    fav = _make_favorite_item(dev_id, is_fav, on_favorite_toggle)
    if fav is not None:
        parent.add(rumps.separator)
        parent.add(fav)
    return parent


def build_dimmer_item(device: dict, store: StateStore,
                      dispatcher: MenuActionDispatcher,
                      client,
                      is_fav: bool = False,
                      on_favorite_toggle: Optional[Callable[[int], None]] = None
                      ) -> rumps.MenuItem:
    """Dimmer: top item shows on/off + dim level using SF Symbols.
    Click toggles on/off; submenu sets brightness.
    """
    from .sf_symbols import sf_image
    from AppKit import NSColor

    name = device.get("name", f"Device {device['id']}")
    props = device.get("properties", {}) or {}
    cur_val = props.get("value", 0)
    is_on = _truthy(props.get("state"))
    is_color = _is_color_controller(device)
    dev_id = int(device["id"])

    def _icon_for(on: bool, val):
        if not on:
            return sf_image("lightbulb", color=NSColor.secondaryLabelColor())
        if is_color:
            # Use the color glyph mapping to pick a tint
            tint_map = {
                "🔴": NSColor.systemRedColor(),
                "🟠": NSColor.systemOrangeColor(),
                "🟡": NSColor.systemYellowColor(),
                "🟢": NSColor.systemGreenColor(),
                "🔵": NSColor.systemBlueColor(),
                "🟣": NSColor.systemPurpleColor(),
                "⚪": NSColor.whiteColor(),
                "🟤": NSColor.brownColor(),
            }
            g = _color_glyph(props)
            return sf_image("lightbulb.fill", color=tint_map.get(g, NSColor.systemYellowColor()))
        return sf_image("lightbulb.fill", color=NSColor.systemYellowColor())

    parent = rumps.MenuItem(f"{name} — {cur_val}%")
    img = _icon_for(is_on, cur_val)
    if img is not None:
        parent._menuitem.setImage_(img)
    state_holder = {"on": is_on}

    def toggle_cb(_):
        new_state = not state_holder["on"]
        state_holder["on"] = new_state
        parent.title = f"{name} — {cur_val}%"
        nimg = _icon_for(new_state, cur_val)
        if nimg is not None:
            parent._menuitem.setImage_(nimg)
        if new_state:
            dispatcher.submit(lambda: client.turn_on(dev_id))
        else:
            dispatcher.submit(lambda: client.turn_off(dev_id))

    parent.set_callback(toggle_cb)

    custom = rumps.MenuItem("Set value…")

    def custom_cb(_):
        win = rumps.Window(
            title=f"Set value for {name}",
            message="Enter a value 0-100:",
            default_text=str(cur_val),
            ok="Set", cancel="Cancel", dimensions=(120, 24),
        )
        resp = win.run()
        if resp.clicked and resp.text.strip():
            try:
                v = int(float(resp.text.strip()))
                v = max(0, min(100, v))
            except ValueError:
                rumps.alert("Invalid value", "Please enter a number 0-100.")
                return
            dispatcher.submit(lambda: client.set_value(dev_id, v))

    # Slider lives inside the "Set value…" submenu.
    try:
        def _on_slider(v: int):
            state_holder["on"] = v > 0
            try:
                parent.title = f"{name} — {v}%"
                nimg = _icon_for(v > 0, v)
                if nimg is not None:
                    parent._menuitem.setImage_(nimg)
            except Exception:
                pass
            dispatcher.submit(lambda: client.set_value(dev_id, v))
        slider_item = _build_slider_item(
            int(cur_val) if str(cur_val).isdigit() else 0,
            _on_slider,
        )
        custom.add(slider_item)
        custom.add(rumps.separator)
        type_it = rumps.MenuItem("Type value…")
        type_it.set_callback(custom_cb)
        custom.add(type_it)
    except Exception as e:
        log.warning("slider unavailable: %s", e)
        custom.set_callback(custom_cb)
    parent.add(custom)

    if is_color:
        parent.add(rumps.separator)
        parent.add(_build_color_submenu(name, dev_id, props, dispatcher, client, store))

    fav = _make_favorite_item(dev_id, is_fav, on_favorite_toggle)
    if fav is not None:
        parent.add(rumps.separator)
        parent.add(fav)
    return parent


# -- Color picker submenu ----------------------------------------------------

# (label, glyph, r, g, b, w)
_COLOR_PRESETS = [
    ("Red",        "🔴", 255,   0,   0, 0),
    ("Orange",     "🟠", 255, 110,   0, 0),
    ("Yellow",     "🟡", 255, 220,   0, 0),
    ("Green",      "🟢",   0, 255,   0, 0),
    ("Cyan",       "🔵",   0, 200, 255, 0),
    ("Blue",       "🔵",   0,   0, 255, 0),
    ("Purple",     "🟣", 160,   0, 255, 0),
    ("Pink",       "🟣", 255,   0, 180, 0),
    ("White",      "⚪", 255, 255, 255, 0),
    ("Warm white", "🟤",   0,   0,   0, 255),
]

# CCT presets: (label, glyph, warmWhite, coldWhite)
_CCT_PRESETS = [
    ("Warm (2700K)",        "🟤", 255,   0),
    ("Soft white (3000K)",  "🟤", 200,  60),
    ("Neutral (4000K)",     "⚪", 130, 130),
    ("Cool (5000K)",        "⚪",  60, 200),
    ("Daylight (6500K)",    "⚪",   0, 255),
]


def _detect_color_kind(cc: dict) -> str:
    """Return 'rgb', 'rgbw', 'cct', or 'unknown' based on component keys."""
    if not isinstance(cc, dict):
        return "unknown"
    keys = set(cc.keys())
    has_rgb = {"red", "green", "blue"}.issubset(keys)
    has_w = bool(keys & {"warmWhite", "coldWhite", "white"})
    if has_rgb and has_w:
        return "rgbw"
    if has_rgb:
        return "rgb"
    if has_w:
        return "cct"
    return "unknown"


def _extract_fav_color(item: dict) -> Optional[tuple]:
    """Extract (r, g, b, w, brightness, name) from an HC3 favorite-color entry.

    Supports both v2 (nested `components`) and v1 (flat r/g/b/w) shapes.
    Returns None if the entry has no usable color data.
    """
    if not isinstance(item, dict):
        return None
    comps = item.get("components")
    if isinstance(comps, dict):
        r = int(comps.get("red", 0) or 0)
        g = int(comps.get("green", 0) or 0)
        b = int(comps.get("blue", 0) or 0)
        w = int(comps.get("warmWhite", comps.get("white", 0)) or 0)
        bright = comps.get("brightness")
    else:
        r = int(item.get("r", 0) or 0)
        g = int(item.get("g", 0) or 0)
        b = int(item.get("b", 0) or 0)
        w = int(item.get("w", 0) or 0)
        bright = item.get("brightness")
    name = item.get("name") or ""
    if r == 0 and g == 0 and b == 0 and w == 0:
        return None
    return r, g, b, w, bright, name


def _build_favorite_colors_submenu(dev_id: int, fav_colors: list[dict],
                                   dispatcher: MenuActionDispatcher,
                                   client) -> Optional[rumps.MenuItem]:
    """Submenu listing the user's HC3 favorite colors, if any."""
    if not fav_colors:
        return None
    from .sf_symbols import sf_image
    from AppKit import NSColor

    parent = rumps.MenuItem("Favorite colors")
    added = 0
    for idx, item in enumerate(fav_colors, 1):
        parsed = _extract_fav_color(item)
        if parsed is None:
            continue
        r, g, b, w, bright, name = parsed
        label = name.strip() or f"Color {item.get('id', idx)}"
        if isinstance(bright, (int, float)) and 0 < int(bright) < 100:
            label = f"{label}  ({int(bright)}%)"
        mi = rumps.MenuItem(label)
        # Tinted swatch using SF Symbols circle.fill.
        try:
            tint = NSColor.colorWithSRGBRed_green_blue_alpha_(
                r / 255.0, g / 255.0, b / 255.0, 1.0)
            img = sf_image("circle.fill", color=tint)
            if img is not None:
                mi._menuitem.setImage_(img)
        except Exception:
            pass
        mi.set_callback(
            lambda _i, r=r, g=g, b=b, w=w:
            dispatcher.submit(lambda: client.set_color(dev_id, r, g, b, w))
        )
        parent.add(mi)
        added += 1
    if added == 0:
        return None
    return parent


def _build_color_submenu(name: str, dev_id: int, props: dict,
                         dispatcher: MenuActionDispatcher, client,
                         store: Optional[StateStore] = None) -> rumps.MenuItem:
    cc = props.get("colorComponents") or {}
    kind = _detect_color_kind(cc)
    root = rumps.MenuItem("Color")

    # HC3 user-curated favorite colors come first when available.
    fav_colors = store.all_favorite_colors() if store is not None else []
    fav_menu = _build_favorite_colors_submenu(dev_id, fav_colors, dispatcher, client)
    if fav_menu is not None:
        root.add(fav_menu)
        root.add(rumps.separator)

    if kind in ("rgb", "rgbw", "unknown"):
        for label, glyph, r, g, b, w in _COLOR_PRESETS:
            mi = rumps.MenuItem(f"{glyph}  {label}")
            mi.set_callback(
                lambda _i, r=r, g=g, b=b, w=w:
                dispatcher.submit(lambda: client.set_color(dev_id, r, g, b, w))
            )
            root.add(mi)
        root.add(rumps.separator)
        custom = rumps.MenuItem("Custom (hex)…")

        def custom_cb(_):
            win = rumps.Window(
                title=f"Set color for {name}",
                message="Enter a hex color (e.g. #ff8800 or ff8800ff for RGBW):",
                default_text="#ffffff",
                ok="Set", cancel="Cancel", dimensions=(160, 24),
            )
            resp = win.run()
            if not (resp.clicked and resp.text.strip()):
                return
            try:
                r, g, b, w = _parse_hex(resp.text.strip())
            except ValueError as e:
                rumps.alert("Invalid color", str(e))
                return
            dispatcher.submit(lambda: client.set_color(dev_id, r, g, b, w))
        custom.set_callback(custom_cb)
        root.add(custom)

        picker_item = rumps.MenuItem("Custom color…")

        def picker_cb(_):
            from . import color_picker
            cc = props.get("colorComponents") or {}
            try:
                init_rgb = (int(cc.get("red", 255) or 0),
                            int(cc.get("green", 255) or 0),
                            int(cc.get("blue", 255) or 0))
            except Exception:
                init_rgb = (255, 255, 255)

            # Debounce: while the user drags the wheel, NSColorPanel fires
            # colorChanged: continuously. Coalesce to ~8 Hz to avoid HC3 spam.
            import time as _time
            state = {"last_sent": 0.0, "pending": None}
            min_interval = 0.12

            def on_pick(r, g, b):
                now = _time.monotonic()
                state["pending"] = (r, g, b)
                if now - state["last_sent"] < min_interval:
                    return
                rr, gg, bb = state["pending"]
                state["last_sent"] = now
                state["pending"] = None
                dispatcher.submit(lambda: client.set_color(dev_id, rr, gg, bb, 0))

            color_picker.show_color_picker(
                title=f"{name} — color",
                initial_rgb=init_rgb,
                on_pick=on_pick,
            )

        picker_item.set_callback(picker_cb)
        root.add(picker_item)

    if kind in ("cct", "rgbw"):
        if kind == "rgbw":
            root.add(rumps.separator)
        # Kelvin slider 2200K..6500K, mapped linearly to warmWhite/coldWhite.
        K_MIN, K_MAX = 2200, 6500
        try:
            cur_ww = int(cc.get("warmWhite", 255) or 0)
            cur_cw = int(cc.get("coldWhite", 0) or 0)
            tot = max(1, cur_ww + cur_cw)
            t = cur_cw / tot
            init_k = int(round(K_MIN + t * (K_MAX - K_MIN)))
            init_k = max(K_MIN, min(K_MAX, init_k))
        except Exception:
            init_k = 3000
        # Preserve brightness if HC3 reports it on this device.
        cur_brightness = cc.get("brightness")

        def _on_kelvin(k: int) -> None:
            t = (k - K_MIN) / (K_MAX - K_MIN)
            cw = int(round(255 * t))
            ww = 255 - cw
            comps = {"warmWhite": ww, "coldWhite": cw}
            if isinstance(cur_brightness, (int, float)):
                comps["brightness"] = int(cur_brightness)
            dispatcher.submit(lambda: client.set_color_components(dev_id, comps))

        temp_menu = rumps.MenuItem("Color temperature")
        try:
            slider_item = _build_slider_item(
                init_k, _on_kelvin,
                min_v=K_MIN, max_v=K_MAX,
                unit=" K", step=100, label_width=64.0,
            )
            temp_menu.add(slider_item)
            temp_menu.add(rumps.separator)
        except Exception as e:
            log.warning("CCT slider unavailable: %s", e)
        for label, glyph, ww, cw in _CCT_PRESETS:
            mi = rumps.MenuItem(f"{glyph}  {label}")
            comps = {"warmWhite": ww, "coldWhite": cw}
            mi.set_callback(
                lambda _i, c=comps:
                dispatcher.submit(lambda: client.set_color_components(dev_id, c))
            )
            temp_menu.add(mi)
        root.add(temp_menu)

    return root


def _parse_hex(text: str) -> tuple:
    """Parse #rgb / #rrggbb / #rrggbbww. Returns (r, g, b, w)."""
    s = text.lstrip("#").strip()
    if len(s) == 3:
        r, g, b = (int(c * 2, 16) for c in s)
        return r, g, b, 0
    if len(s) == 6:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 0
    if len(s) == 8:
        return (int(s[0:2], 16), int(s[2:4], 16),
                int(s[4:6], 16), int(s[6:8], 16))
    raise ValueError("Use 3, 6, or 8 hex digits (RGB, RRGGBB, or RRGGBBWW).")


def build_shutter_item(device: dict, store: StateStore,
                       dispatcher: MenuActionDispatcher,
                       client,
                       is_fav: bool = False,
                       on_favorite_toggle: Optional[Callable[[int], None]] = None
                       ) -> rumps.MenuItem:
    """Shutter: `value` is % open. Submenu: open/close + presets + custom value."""
    name = device.get("name", f"Device {device['id']}")
    props = device.get("properties", {}) or {}
    cur_val = props.get("value", 0)
    parent = rumps.MenuItem(f"{name} — {cur_val}% open")
    _set_icon(parent, "blinds.horizontal.closed")
    dev_id = int(device["id"])

    open_item = rumps.MenuItem("Open")
    open_item.set_callback(lambda _: dispatcher.submit(lambda: client.call_action(dev_id, "open")))
    parent.add(open_item)

    close_item = rumps.MenuItem("Close")
    close_item.set_callback(lambda _: dispatcher.submit(lambda: client.call_action(dev_id, "close")))
    parent.add(close_item)

    stop_item = rumps.MenuItem("Stop")
    stop_item.set_callback(lambda _: dispatcher.submit(lambda: client.call_action(dev_id, "stop")))
    parent.add(stop_item)

    parent.add(rumps.separator)

    def make_preset(val: int):
        preset = rumps.MenuItem(f"{val}% open")
        preset.state = 1 if str(cur_val) == str(val) else 0

        def cb(_):
            dispatcher.submit(lambda: client.set_value(dev_id, val))
        preset.set_callback(cb)
        return preset

    for v in (0, 25, 50, 75, 100):
        parent.add(make_preset(v))

    custom = rumps.MenuItem("Set position…")

    def custom_cb(_):
        win = rumps.Window(
            title=f"Set position for {name}",
            message="Enter % open 0-100:",
            default_text=str(cur_val),
            ok="Set", cancel="Cancel", dimensions=(120, 24),
        )
        resp = win.run()
        if resp.clicked and resp.text.strip():
            try:
                v = int(float(resp.text.strip()))
                v = max(0, min(100, v))
            except ValueError:
                rumps.alert("Invalid value", "Please enter a number 0-100.")
                return
            dispatcher.submit(lambda: client.set_value(dev_id, v))

    try:
        def _on_slider(v: int):
            try:
                parent.title = f"{name} — {v}% open"
            except Exception:
                pass
            dispatcher.submit(lambda: client.set_value(dev_id, v))
        slider_item = _build_slider_item(
            int(cur_val) if str(cur_val).isdigit() else 0,
            _on_slider,
        )
        custom.add(slider_item)
        custom.add(rumps.separator)
        type_it = rumps.MenuItem("Type value…")
        type_it.set_callback(custom_cb)
        custom.add(type_it)
    except Exception as e:
        log.warning("slider unavailable: %s", e)
        custom.set_callback(custom_cb)
    parent.add(custom)

    fav = _make_favorite_item(dev_id, is_fav, on_favorite_toggle)
    if fav is not None:
        parent.add(rumps.separator)
        parent.add(fav)
    return parent


_SENSOR_GLYPH = {
    "temp_sensor": GLYPH_TEMP,
    "lux_sensor": GLYPH_LUX,
    "humidity_sensor": GLYPH_HUMIDITY,
}


def _format_sensor_value(val) -> str:
    """Format numeric sensor value with at most 2 decimals; pass through strings."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}"


_SENSOR_SF = {
    "temp_sensor": ("thermometer.medium", lambda: NSColor.systemOrangeColor()),
    "lux_sensor": ("sun.max.fill", lambda: NSColor.systemYellowColor()),
    "humidity_sensor": ("humidity.fill", lambda: NSColor.systemBlueColor()),
}


def build_sensor_item(device: dict, kind: str,
                      is_fav: bool = False,
                      on_favorite_toggle: Optional[Callable[[int], None]] = None
                      ) -> rumps.MenuItem:
    name = device.get("name", f"Device {device['id']}")
    props = device.get("properties", {}) or {}
    val = props.get("value", "?")
    unit = props.get("unit") or {
        "temp_sensor": "°C",
        "lux_sensor": "lux",
        "humidity_sensor": "%",
    }.get(kind, "")
    if kind == "motion_sensor":
        active = _truthy(val)
        label = f"{name} — {'Motion' if active else 'Idle'}"
        item = rumps.MenuItem(label)
        _set_icon(item, "figure.walk" if active else "figure.stand",
                  color=NSColor.systemGreenColor() if active else NSColor.secondaryLabelColor())
    else:
        v_text = _format_sensor_value(val)
        label = f"{name} — {v_text} {unit}".rstrip()
        item = rumps.MenuItem(label)
        sym = _SENSOR_SF.get(kind)
        if sym is not None:
            _set_icon(item, sym[0], color=sym[1]())
    fav = _make_favorite_item(int(device["id"]), is_fav, on_favorite_toggle)
    if fav is not None:
        item.add(fav)
    else:
        # Give a no-op callback so the row is not greyed out.
        item.set_callback(lambda _i: None)
    return item


def build_thermostat_item(device: dict, store: StateStore,
                          dispatcher: MenuActionDispatcher,
                          client,
                          is_fav: bool = False,
                          on_favorite_toggle: Optional[Callable[[int], None]] = None
                          ) -> rumps.MenuItem:
    name = device.get("name", f"Device {device['id']}")
    props = device.get("properties", {}) or {}
    setpoint = props.get("heatingThermostatSetpoint", props.get("targetLevel", "?"))
    parent = rumps.MenuItem(f"{name} — set {setpoint}°")
    _set_icon(parent, "flame.fill", color=NSColor.systemOrangeColor())
    dev_id = int(device["id"])

    for sp in (16, 18, 19, 20, 21, 22, 23):
        item = rumps.MenuItem(f"{sp}°C")

        def make_cb(value=sp):
            def cb(_):
                dispatcher.submit(
                    lambda: client.set_thermostat_setpoint(dev_id, float(value))
                )
            return cb

        item.set_callback(make_cb())
        parent.add(item)

    custom = rumps.MenuItem("Set setpoint…")

    def custom_cb(_):
        win = rumps.Window(
            title=f"Setpoint for {name}",
            message="Enter setpoint (°C):",
            default_text=str(setpoint),
            ok="Set", cancel="Cancel", dimensions=(120, 24),
        )
        resp = win.run()
        if resp.clicked and resp.text.strip():
            try:
                v = float(resp.text.strip())
            except ValueError:
                rumps.alert("Invalid value", "Please enter a number.")
                return
            dispatcher.submit(lambda: client.set_thermostat_setpoint(dev_id, v))

    custom.set_callback(custom_cb)
    parent.add(custom)

    fav = _make_favorite_item(dev_id, is_fav, on_favorite_toggle)
    if fav is not None:
        parent.add(rumps.separator)
        parent.add(fav)
    return parent


# -- Top-level menu builder ---------------------------------------------------

def build_device_item(device: dict, store: StateStore,
                      dispatcher: MenuActionDispatcher,
                      client,
                      is_fav: bool = False,
                      on_favorite_toggle: Optional[Callable[[int], None]] = None
                      ) -> Optional[rumps.MenuItem]:
    kind = classify(device)
    if kind == "switch":
        return build_switch_item(device, store, dispatcher, client,
                                 is_fav=is_fav, on_favorite_toggle=on_favorite_toggle)
    if kind == "dimmer":
        return build_dimmer_item(device, store, dispatcher, client,
                                 is_fav=is_fav, on_favorite_toggle=on_favorite_toggle)
    if kind == "shutter":
        return build_shutter_item(device, store, dispatcher, client,
                                  is_fav=is_fav, on_favorite_toggle=on_favorite_toggle)
    if kind in ("temp_sensor", "lux_sensor", "humidity_sensor", "motion_sensor"):
        return build_sensor_item(device, kind,
                                 is_fav=is_fav, on_favorite_toggle=on_favorite_toggle)
    if kind == "thermostat":
        return build_thermostat_item(device, store, dispatcher, client,
                                     is_fav=is_fav, on_favorite_toggle=on_favorite_toggle)
    return None


def build_root_menu(store: StateStore,
                    favorites: list[int],
                    dispatcher: MenuActionDispatcher,
                    client,
                    on_refresh: Callable[[], None],
                    on_prefs: Callable[[], None],
                    partitions: Optional[list[dict]] = None,
                    on_arm: Optional[Callable[[int], None]] = None,
                    on_disarm: Optional[Callable[[int], None]] = None,
                    on_arm_all: Optional[Callable[[], None]] = None,
                    on_disarm_all: Optional[Callable[[], None]] = None,
                    profiles: Optional[tuple[list[dict], Optional[int]]] = None,
                    on_profile: Optional[Callable[[int], None]] = None,
                    scenes: Optional[list[dict]] = None,
                    on_scene: Optional[Callable[[int], None]] = None,
                    activity: Optional[list[dict]] = None,
                    attention: Optional[list[dict]] = None,
                    debug_messages: Optional[list[dict]] = None,
                    diagnostics: Optional[tuple[dict, list[float]]] = None,
                    on_favorite_toggle: Optional[Callable[[int], None]] = None,
                    on_check_updates: Optional[Callable[[], None]] = None,
                    version: Optional[str] = None,
                    ) -> list:
    """Return a list suitable for assigning to rumps.App.menu."""
    devices = store.all_devices()

    # Favorites
    fav_set = set(int(x) for x in favorites)
    fav_devs = [d for d in devices if int(d["id"]) in fav_set]
    fav_menu = rumps.MenuItem("Favorites")
    if fav_devs:
        for d in sorted(fav_devs, key=lambda x: x.get("name", "")):
            it = build_device_item(d, store, dispatcher, client,
                                   is_fav=True,
                                   on_favorite_toggle=on_favorite_toggle)
            if it is not None:
                fav_menu.add(it)
    else:
        empty = rumps.MenuItem("(none — use ☆ in any device submenu)")
        empty.set_callback(None)
        fav_menu.add(empty)

    # Rooms → Type → Device
    type_labels = {
        "switch": "Switches",
        "dimmer": "Dimmers",
        "shutter": "Shutters",
        "temp_sensor": "Temperature",
        "lux_sensor": "Light",
        "humidity_sensor": "Humidity",
        "motion_sensor": "Motion",
        "thermostat": "Thermostats",
    }

    by_room_type: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for d in devices:
        k = classify(d)
        if k is None:
            continue
        by_room_type[int(d.get("roomID", 0) or 0)][k].append(d)

    rooms_menu = rumps.MenuItem("Rooms")
    for room_id in sorted(by_room_type.keys(), key=lambda r: store.room_name(r).lower()):
        rname = store.room_name(room_id)
        room_sub = rumps.MenuItem(rname)
        type_buckets = by_room_type[room_id]
        for k, label in type_labels.items():
            devs = type_buckets.get(k)
            if not devs:
                continue
            type_sub = rumps.MenuItem(label)
            for d in sorted(devs, key=lambda x: x.get("name", "")):
                it = build_device_item(
                    d, store, dispatcher, client,
                    is_fav=int(d["id"]) in fav_set,
                    on_favorite_toggle=on_favorite_toggle,
                )
                if it is not None:
                    type_sub.add(it)
            room_sub.add(type_sub)
        rooms_menu.add(room_sub)

    refresh_item = rumps.MenuItem("Refresh now", callback=lambda _: on_refresh())
    prefs_item = rumps.MenuItem("Preferences…", callback=lambda _: on_prefs())
    update_item: Optional[rumps.MenuItem] = None
    if on_check_updates is not None:
        title = "Check for updates…"
        if version:
            title = f"Check for updates…  (v{version})"
        update_item = rumps.MenuItem(title, callback=lambda _: on_check_updates())
        _set_icon(update_item, "arrow.down.circle")
    quit_item = rumps.MenuItem("Quit HC3 Menu", callback=lambda _: rumps.quit_application(),
                               key="q")

    result = [fav_menu, rooms_menu]
    alarm_menu = build_alarm_menu(
        partitions or [], dispatcher, client,
        on_arm=on_arm, on_disarm=on_disarm,
        on_arm_all=on_arm_all, on_disarm_all=on_disarm_all,
    )
    extras = []
    if alarm_menu is not None:
        extras.append(alarm_menu)
    if profiles is not None and on_profile is not None:
        prof_list, active_id = profiles
        prof_menu = build_profile_menu(prof_list, active_id, on_profile)
        if prof_menu is not None:
            extras.append(prof_menu)
    if scenes and on_scene is not None:
        scenes_menu = build_scenes_menu(scenes, store, on_scene)
        if scenes_menu is not None:
            extras.append(scenes_menu)
    if attention:
        att_menu = build_attention_menu(attention, store)
        if att_menu is not None:
            extras.append(att_menu)
    if activity:
        act_menu = build_activity_menu(activity)
        if act_menu is not None:
            extras.append(act_menu)
    if debug_messages:
        dbg_menu = build_debug_messages_menu(debug_messages)
        if dbg_menu is not None:
            extras.append(dbg_menu)
    if diagnostics is not None:
        diag, cpu_pcts = diagnostics
        diag_menu = build_diagnostics_menu(diag, cpu_pcts)
        if diag_menu is not None:
            extras.append(diag_menu)
    if extras:
        result.append(None)
        result.extend(extras)
    result += [None, refresh_item, prefs_item]
    if update_item is not None:
        result.append(update_item)
    result += [None, quit_item]
    return result


def build_alarm_menu(partitions: list[dict],
                     dispatcher: MenuActionDispatcher,
                     client,
                     on_arm: Optional[Callable[[int], None]] = None,
                     on_disarm: Optional[Callable[[int], None]] = None,
                     on_arm_all: Optional[Callable[[], None]] = None,
                     on_disarm_all: Optional[Callable[[], None]] = None) -> Optional[rumps.MenuItem]:
    if not partitions:
        return None

    armed_count = sum(1 for p in partitions if p.get("armed"))
    pending_count = sum(1 for p in partitions if p.get("_pending_arm") or p.get("_pending_disarm"))
    breached = any(p.get("breached") for p in partitions)
    total = len(partitions)
    if breached:
        top_sym, top_color = "exclamationmark.triangle.fill", NSColor.systemRedColor()
        summary = "BREACHED"
    elif pending_count:
        top_sym, top_color = "hourglass", NSColor.systemYellowColor()
        summary = f"{armed_count}/{total} armed (pending…)"
    elif armed_count == total:
        top_sym, top_color = "lock.fill", NSColor.systemRedColor()
        summary = "All armed"
    elif armed_count == 0:
        top_sym, top_color = "lock.open.fill", NSColor.systemGreenColor()
        summary = "All disarmed"
    else:
        top_sym, top_color = "lock.fill", NSColor.systemRedColor()
        summary = f"{armed_count}/{total} armed"

    root = rumps.MenuItem(f"Alarm — {summary}")
    _set_icon(root, top_sym, color=top_color)

    for p in sorted(partitions, key=lambda x: (x.get("name") or "").lower()):
        pid = int(p["id"])
        name = p.get("name", f"Partition {pid}")
        armed = bool(p.get("armed"))
        bre = bool(p.get("breached"))
        pending_arm = bool(p.get("_pending_arm"))
        pending_disarm = bool(p.get("_pending_disarm"))
        if bre:
            sym, color, state = "exclamationmark.triangle.fill", NSColor.systemRedColor(), "Breached"
        elif pending_arm and not armed:
            sym, color, state = "hourglass", NSColor.systemYellowColor(), "Arming…"
        elif pending_disarm and armed:
            sym, color, state = "hourglass", NSColor.systemYellowColor(), "Disarming…"
        elif armed:
            sym, color, state = "lock.fill", NSColor.systemRedColor(), "Armed"
        else:
            sym, color, state = "lock.open.fill", NSColor.systemGreenColor(), "Disarmed"
        sub = rumps.MenuItem(f"{name} — {state}")
        _set_icon(sub, sym, color=color)
        arm_it = rumps.MenuItem("Arm")
        if on_arm is not None:
            arm_it.set_callback(lambda _i, i=pid: on_arm(i))
        else:
            arm_it.set_callback(
                lambda _i, i=pid: dispatcher.submit(lambda: client.arm_partition(i)))
        disarm_it = rumps.MenuItem("Disarm")
        if on_disarm is not None:
            disarm_it.set_callback(lambda _i, i=pid: on_disarm(i))
        else:
            disarm_it.set_callback(
                lambda _i, i=pid: dispatcher.submit(lambda: client.disarm_partition(i)))
        sub.add(arm_it)
        sub.add(disarm_it)
        root.add(sub)

    if len(partitions) > 1:
        root.add(rumps.separator)
        arm_all = rumps.MenuItem("Arm all")
        if on_arm_all is not None:
            arm_all.set_callback(lambda _: on_arm_all())
        else:
            arm_all.set_callback(
                lambda _: dispatcher.submit(client.arm_all_partitions))
        disarm_all = rumps.MenuItem("Disarm all")
        if on_disarm_all is not None:
            disarm_all.set_callback(lambda _: on_disarm_all())
        else:
            disarm_all.set_callback(
                lambda _: dispatcher.submit(client.disarm_all_partitions))
        root.add(arm_all)
        root.add(disarm_all)
    return root


def build_attention_menu(attention: list[dict], store: StateStore) -> Optional[rumps.MenuItem]:
    """List of devices that are dead or have low battery."""
    if not attention:
        return None
    dead = [d for d in attention if (d.get("properties") or {}).get("dead")]
    low_batt = [d for d in attention
                if not (d.get("properties") or {}).get("dead")]
    root = rumps.MenuItem(f"Attention ({len(attention)})")
    _set_icon(root, "exclamationmark.triangle.fill", color=NSColor.systemOrangeColor())

    def _row(d: dict, sym: str, color, suffix: str) -> rumps.MenuItem:
        name = d.get("name", f"Device {d.get('id')}")
        room = store.room_name(int(d.get("roomID", 0) or 0))
        it = rumps.MenuItem(f"{name}  ({room})  — {suffix}")
        _set_icon(it, sym, color=color)
        it.set_callback(lambda _i: None)
        return it

    for d in sorted(dead, key=lambda x: (x.get("name") or "").lower()):
        root.add(_row(d, "bolt.slash.fill", NSColor.systemRedColor(), "dead"))

    if dead and low_batt:
        root.add(rumps.separator)

    for d in sorted(low_batt, key=lambda x: ((x.get("properties") or {}).get("batteryLevel") or 0)):
        batt = (d.get("properties") or {}).get("batteryLevel")
        try:
            batt_n = float(batt)
        except (TypeError, ValueError):
            batt_n = 0.0
        if batt_n <= 5:
            sym, color = "battery.0percent", NSColor.systemRedColor()
        elif batt_n <= 10:
            sym, color = "battery.25percent", NSColor.systemRedColor()
        else:
            sym, color = "battery.25percent", NSColor.systemOrangeColor()
        root.add(_row(d, sym, color, f"battery {batt_n:.0f}%"))
    return root


def _format_activity_time(ts: float) -> str:
    import time as _t
    delta = _t.time() - ts
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta/60)}m ago"
    if delta < 86400:
        return _t.strftime("%H:%M", _t.localtime(ts))
    return _t.strftime("%b %d %H:%M", _t.localtime(ts))


_ACTIVITY_SYMBOLS = {
    "alarm": ("lock.fill", lambda: NSColor.systemRedColor()),
    "breach": ("exclamationmark.triangle.fill", lambda: NSColor.systemRedColor()),
    "profile": ("person.crop.circle.fill", lambda: NSColor.systemBlueColor()),
    "device": ("bolt.fill", lambda: NSColor.systemYellowColor()),
}


def build_activity_menu(activity: list[dict]) -> Optional[rumps.MenuItem]:
    if not activity:
        return None
    root = rumps.MenuItem(f"Recent activity ({len(activity)})")
    _set_icon(root, "clock.arrow.circlepath", color=NSColor.systemTealColor())
    for ev in activity:
        when = _format_activity_time(ev.get("ts", 0))
        text = ev.get("text", "?")
        it = rumps.MenuItem(f"{when}  —  {text}")
        sym = _ACTIVITY_SYMBOLS.get(ev.get("kind", ""))
        if sym is not None:
            _set_icon(it, sym[0], color=sym[1]())
        it.set_callback(lambda _i: None)
        root.add(it)
    return root


def build_debug_messages_menu(messages: list[dict]) -> Optional[rumps.MenuItem]:
    """QA error/warning messages from /debugMessages."""
    if not messages:
        return None
    err_count = sum(1 for m in messages if m.get("type") == "error")
    warn_count = sum(1 for m in messages if m.get("type") == "warning")
    label_parts = []
    if err_count:
        label_parts.append(f"{err_count} err")
    if warn_count:
        label_parts.append(f"{warn_count} warn")
    label = "QA log — " + (", ".join(label_parts) if label_parts else f"{len(messages)}")
    root = rumps.MenuItem(label)
    if err_count:
        _set_icon(root, "exclamationmark.octagon.fill", color=NSColor.systemRedColor())
    else:
        _set_icon(root, "exclamationmark.bubble.fill", color=NSColor.systemOrangeColor())

    for m in messages:
        ts = m.get("timestamp", 0)
        try:
            ts_f = float(ts)
            # HC3 timestamps are seconds since epoch
            when = _format_activity_time(ts_f)
        except (TypeError, ValueError):
            when = "?"
        mtype = m.get("type", "")
        tag = m.get("tag") or ""
        msg = (m.get("message") or "").strip().replace("\n", " ")
        if len(msg) > 120:
            msg = msg[:117] + "…"
        prefix = f"[{tag}] " if tag else ""
        it = rumps.MenuItem(f"{when}  —  {prefix}{msg}")
        if mtype == "error":
            _set_icon(it, "exclamationmark.octagon.fill", color=NSColor.systemRedColor())
        elif mtype == "warning":
            _set_icon(it, "exclamationmark.triangle.fill", color=NSColor.systemOrangeColor())
        else:
            _set_icon(it, "info.circle", color=NSColor.secondaryLabelColor())
        it.set_callback(lambda _i: None)
        root.add(it)
    return root


def build_scenes_menu(scenes: list[dict], store: StateStore,
                      on_scene: Callable[[int], None]) -> Optional[rumps.MenuItem]:
    """Scenes grouped by room. Hidden scenes are skipped."""
    visible = [s for s in scenes if not s.get("hidden")]
    if not visible:
        return None
    root = rumps.MenuItem(f"Scenes ({len(visible)})")
    _set_icon(root, "wand.and.stars", color=NSColor.systemPurpleColor())

    by_room: dict[int, list[dict]] = defaultdict(list)
    for s in visible:
        by_room[int(s.get("roomID", 0) or 0)].append(s)

    multi_room = len(by_room) > 1
    if multi_room:
        for room_id in sorted(by_room.keys(), key=lambda r: store.room_name(r).lower()):
            sub = rumps.MenuItem(store.room_name(room_id))
            for s in sorted(by_room[room_id], key=lambda x: (x.get("name") or "").lower()):
                sid = int(s["id"])
                it = rumps.MenuItem(s.get("name", f"Scene {sid}"))
                _set_icon(it, "play.circle", color=NSColor.systemPurpleColor())
                it.set_callback(lambda _i, i=sid: on_scene(i))
                sub.add(it)
            root.add(sub)
    else:
        for s in sorted(visible, key=lambda x: (x.get("name") or "").lower()):
            sid = int(s["id"])
            it = rumps.MenuItem(s.get("name", f"Scene {sid}"))
            _set_icon(it, "play.circle", color=NSColor.systemPurpleColor())
            it.set_callback(lambda _i, i=sid: on_scene(i))
            root.add(it)
    return root


def build_profile_menu(profiles, active_id, on_profile):
    if not profiles:
        return None
    active_name = None
    for p in profiles:
        if active_id is not None and int(p.get("id", -1)) == int(active_id):
            active_name = p.get("name", f"Profile {active_id}")
            break
    root = rumps.MenuItem(f"Profile — {active_name or 'unknown'}")
    _set_icon(root, "person.crop.circle.fill", color=NSColor.systemBlueColor())
    for p in sorted(profiles, key=lambda x: (x.get("name") or "").lower()):
        pid = int(p["id"])
        is_active = active_id is not None and pid == int(active_id)
        item = rumps.MenuItem(p.get("name", f"Profile {pid}"))
        if is_active:
            _set_icon(item, "checkmark.circle.fill", color=NSColor.systemGreenColor())
        else:
            _set_icon(item, "circle", color=NSColor.secondaryLabelColor())
        item.set_callback(lambda _i, i=pid: on_profile(i))
        root.add(item)
    return root


def build_diagnostics_menu(diag, cpu_pcts):
    if not diag:
        return None
    mem = diag.get("memory") or {}
    storage_internal = (diag.get("storage") or {}).get("internal") or []
    cpu_load = diag.get("cpuLoad") or []

    mem_used = mem.get("used")
    mem_free = mem.get("free")
    if cpu_pcts:
        cpu_avg = sum(cpu_pcts) / len(cpu_pcts)
    else:
        cpu_avg = None

    parts = []
    if cpu_avg is not None:
        parts.append(f"CPU {cpu_avg:.0f}%")
    if mem_used is not None:
        parts.append(f"RAM {int(mem_used)}%")
    if storage_internal:
        max_disk = max((s.get("used") or 0) for s in storage_internal)
        parts.append(f"Disk {max_disk:.0f}%")
    summary = "  ".join(parts) if parts else "no data"
    root = rumps.MenuItem(f"Diagnostics — {summary}")
    _set_icon(root, "chart.bar.fill", color=NSColor.systemTealColor())

    # CPU per-core
    if cpu_pcts:
        for i, pct in enumerate(cpu_pcts):
            it = rumps.MenuItem(f"CPU{i} — {pct:.0f}%")
            _set_icon(it, "cpu.fill", color=NSColor.systemPurpleColor())
            it.set_callback(lambda _i: None)
            root.add(it)
    elif cpu_load:
        # No delta yet (first sample); show placeholder
        for i in range(len(cpu_load)):
            it = rumps.MenuItem(f"CPU{i} — sampling…")
            _set_icon(it, "cpu.fill", color=NSColor.systemPurpleColor())
            it.set_callback(lambda _i: None)
            root.add(it)

    root.add(rumps.separator)
    if mem:
        used = mem.get("used", 0)
        free = mem.get("free", 0)
        cache = mem.get("cache", 0)
        buffers = mem.get("buffers", 0)
        ram_it = rumps.MenuItem(
            f"RAM — used {used}%  free {free}%  cache {cache}%  buf {buffers}%")
        _set_icon(ram_it, "memorychip.fill", color=NSColor.systemBlueColor())
        ram_it.set_callback(lambda _i: None)
        root.add(ram_it)

    for s in storage_internal:
        name = s.get("name", "?")
        used = s.get("used") or 0
        it = rumps.MenuItem(f"Storage {name} — {used:.1f}% used")
        _set_icon(it, "internaldrive.fill", color=NSColor.systemGrayColor())
        it.set_callback(lambda _i: None)
        root.add(it)

    return root
