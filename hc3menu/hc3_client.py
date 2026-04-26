"""Thin HC3 REST client (Basic Auth, requests.Session)."""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth

from .config import HC3Credentials

log = logging.getLogger(__name__)


class HC3Error(Exception):
    """Raised for HC3 API failures."""


class HC3Client:
    def __init__(self, creds: HC3Credentials, request_timeout: float = 10.0):
        self.creds = creds
        self.request_timeout = request_timeout
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(creds.user, creds.password)
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Fibaro-Version": "2",
        })

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.creds.base_url + path

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 timeout: Optional[float] = None,
                 extra_headers: Optional[dict] = None) -> Any:
        url = self._url(path)
        try:
            resp = self.session.request(
                method, url, json=json_body,
                headers=extra_headers,
                timeout=timeout if timeout is not None else self.request_timeout,
            )
        except requests.RequestException as e:
            raise HC3Error(f"{method} {path} failed: {e}") from e
        if resp.status_code >= 400:
            raise HC3Error(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # -- Discovery -----------------------------------------------------
    def get_devices(self, *, type_: Optional[str] = None,
                    room_id: Optional[int] = None,
                    interface: Optional[str] = None) -> list[dict]:
        params = []
        if type_:
            params.append(f"type={type_}")
        if room_id is not None:
            params.append(f"roomID={room_id}")
        if interface:
            params.append(f"interface={interface}")
        qs = ("?" + "&".join(params)) if params else ""
        return self._request("GET", "/devices" + qs) or []

    def get_device(self, device_id: int) -> dict:
        return self._request("GET", f"/devices/{device_id}")

    def get_property(self, device_id: int, name: str) -> Any:
        return self._request("GET", f"/devices/{device_id}/properties/{name}")

    def get_rooms(self) -> list[dict]:
        return self._request("GET", "/rooms") or []

    # -- Control -------------------------------------------------------
    def call_action(self, device_id: int, action: str, args: Optional[list] = None) -> Any:
        body = {"args": args or []}
        return self._request("POST", f"/devices/{device_id}/action/{action}", json_body=body)

    def turn_on(self, device_id: int) -> Any:
        return self.call_action(device_id, "turnOn")

    def turn_off(self, device_id: int) -> Any:
        return self.call_action(device_id, "turnOff")

    def set_value(self, device_id: int, value: Any) -> Any:
        return self.call_action(device_id, "setValue", [value])

    def set_thermostat_setpoint(self, device_id: int, value: float,
                                mode: str = "Heat") -> Any:
        action = "setHeatingThermostatSetpoint" if mode == "Heat" else "setCoolingThermostatSetpoint"
        return self.call_action(device_id, action, [value])

    def set_color(self, device_id: int, r: int, g: int, b: int, w: int = 0) -> Any:
        """Set RGB(W) color on a color controller."""
        return self.call_action(device_id, "setColor", [int(r), int(g), int(b), int(w)])

    def set_color_components(self, device_id: int, components: dict) -> Any:
        """Set per-channel color components, e.g. {'red':255,'warmWhite':128}."""
        return self.call_action(device_id, "setColorComponents", [components])

    def get_favorite_colors(self) -> list[dict]:
        """List user's favorite colors from the HC3 panel.

        v2 returns items shaped like
            {"id": 1, "name": "...", "created": ..., "modified": ...,
             "components": {"red":255, "green":100, "blue":50,
                            "warmWhite":0, "coldWhite":0, "brightness":100}}
        Older firmware (v1) returns flat r/g/b/w. We try v2 first and
        fall back to v1 to stay compatible.
        """
        for path in ("/panels/favoriteColors/v2", "/panels/favoriteColors"):
            try:
                data = self._request("GET", path)
            except HC3Error:
                continue
            if not data:
                continue
            # HC3 sometimes wraps as {"items":[...]}; normalise to list.
            if isinstance(data, dict) and "items" in data:
                data = data.get("items") or []
            if isinstance(data, list):
                return data
        return []

    # -- Alarm partitions ---------------------------------------------
    def _pin_headers(self) -> Optional[dict]:
        pin = (self.creds.pin or "").strip()
        return {"Fibaro-User-PIN": pin} if pin else None

    def get_partitions(self) -> list[dict]:
        """List all alarm partitions. Each: id, name, armed, breached, ..."""
        return self._request("GET", "/alarms/v1/partitions") or []

    def get_breached_partitions(self) -> list[int]:
        return self._request("GET", "/alarms/v1/partitions/breached") or []

    def arm_partition(self, partition_id: int) -> Any:
        return self._request(
            "POST", f"/alarms/v1/partitions/{partition_id}/actions/arm",
            json_body={}, extra_headers=self._pin_headers())

    def disarm_partition(self, partition_id: int) -> Any:
        return self._request(
            "DELETE", f"/alarms/v1/partitions/{partition_id}/actions/arm",
            extra_headers=self._pin_headers())

    def arm_all_partitions(self) -> Any:
        return self._request(
            "POST", "/alarms/v1/partitions/actions/arm",
            json_body={}, extra_headers=self._pin_headers())

    def disarm_all_partitions(self) -> Any:
        return self._request(
            "DELETE", "/alarms/v1/partitions/actions/arm",
            extra_headers=self._pin_headers())

    # -- Profiles -----------------------------------------------------
    def get_profiles(self) -> dict:
        """Returns {activeProfile: int, profiles: [...]}.
        Older firmware returns a bare list; we normalize."""
        data = self._request("GET", "/profiles")
        if isinstance(data, list):
            return {"activeProfile": None, "profiles": data}
        if isinstance(data, dict):
            data.setdefault("profiles", [])
            data.setdefault("activeProfile", None)
            return data
        return {"activeProfile": None, "profiles": []}

    def set_active_profile(self, profile_id: int) -> Any:
        return self._request(
            "POST", f"/profiles/activeProfile/{int(profile_id)}",
            json_body={})

    # -- Scenes -------------------------------------------------------
    def get_scenes(self) -> list[dict]:
        """List all scenes. Each: id, name, roomID, type, isLua, hidden, ..."""
        return self._request("GET", "/scenes") or []

    def run_scene(self, scene_id: int) -> Any:
        """Execute a scene by id."""
        return self._request(
            "POST", f"/scenes/{int(scene_id)}/execute",
            json_body={})

    # -- Diagnostics --------------------------------------------------
    def get_diagnostics(self) -> dict:
        data = self._request("GET", "/diagnostics")
        return data if isinstance(data, dict) else {}

    # -- Debug messages -----------------------------------------------
    def get_debug_messages(self, *, types: Optional[list[str]] = None,
                           offset: int = 20,
                           last: Optional[int] = None,
                           from_: Optional[int] = None,
                           to: Optional[int] = None) -> dict:
        """Fetch QA debug messages.

        Returns dict {nextLast: int, messages: [{id, timestamp, type, tag, message}]}.
        Default returns last 20 of any type. Filter with `types=['warning','error']`.
        """
        params = []
        if types:
            params.append("types=" + ",".join(types))
        if last is not None:
            params.append(f"last={int(last)}")
        if from_ is not None:
            params.append(f"from={int(from_)}")
        if to is not None:
            params.append(f"to={int(to)}")
        params.append(f"offset={int(offset)}")
        qs = "?" + "&".join(params)
        data = self._request("GET", "/debugMessages" + qs)
        if not isinstance(data, dict):
            return {"nextLast": 0, "messages": []}
        data.setdefault("messages", [])
        data.setdefault("nextLast", 0)
        return data

    # -- Refresh states -----------------------------------------------
    def refresh_states(self, last: int = 0, *, timeout: Optional[float] = None) -> dict:
        """Long-polls /refreshStates. Returns dict with at least 'last', 'changes', 'events'."""
        data = self._request("GET", f"/refreshStates?last={last}", timeout=timeout)
        if not isinstance(data, dict):
            return {"last": last, "changes": [], "events": []}
        data.setdefault("changes", [])
        data.setdefault("events", [])
        data.setdefault("last", last)
        return data

    def test_connection(self) -> tuple[bool, str]:
        try:
            info = self._request("GET", "/settings/info", timeout=5)
            name = (info or {}).get("serialNumber") or (info or {}).get("hcName") or "HC3"
            return True, f"Connected to {name}"
        except HC3Error as e:
            return False, str(e)
