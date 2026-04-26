"""In-memory state cache + background poller for HC3 /refreshStates."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .hc3_client import HC3Client, HC3Error

log = logging.getLogger(__name__)


class StateStore:
    """Thread-safe cache of devices, rooms and live property values."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.devices: dict[int, dict] = {}
        self.rooms: dict[int, dict] = {}
        self.partitions: dict[int, dict] = {}
        self.profiles: list[dict] = []
        self.active_profile_id: Optional[int] = None
        self.diagnostics: dict = {}
        self.cpu_pcts: list[float] = []  # per-core busy % (delta-based)
        self._prev_cpu: list[dict] = []
        self.scenes: list[dict] = []
        self.last_refresh: int = 0
        # Recent activity ring buffer: list of dicts
        # {ts: float, kind: str, dev_id: int|None, dev_name: str, text: str}
        self._activity: deque = deque(maxlen=50)
        # QA debug messages (errors/warnings) ring buffer
        self._debug_msgs: deque = deque(maxlen=50)
        self._debug_seen_ids: set = set()
        # Connection state
        self._connected: bool = False
        self._last_error: str = ""

    def replace_devices(self, devs: list[dict]) -> None:
        with self._lock:
            self.devices = {int(d["id"]): d for d in devs if "id" in d}

    def replace_rooms(self, rooms: list[dict]) -> None:
        with self._lock:
            self.rooms = {int(r["id"]): r for r in rooms if "id" in r}

    def replace_partitions(self, parts: list[dict]) -> None:
        with self._lock:
            self.partitions = {int(p["id"]): p for p in parts if "id" in p}

    def all_partitions(self) -> list[dict]:
        with self._lock:
            return list(self.partitions.values())

    def update_partition(self, partition_id: int, **fields) -> None:
        with self._lock:
            p = self.partitions.get(int(partition_id))
            if p:
                p.update(fields)

    def replace_profiles(self, data: dict) -> None:
        with self._lock:
            self.profiles = list(data.get("profiles") or [])
            ap = data.get("activeProfile")
            self.active_profile_id = int(ap) if ap is not None else None

    def all_profiles(self) -> tuple[list[dict], Optional[int]]:
        with self._lock:
            return list(self.profiles), self.active_profile_id

    def set_active_profile(self, profile_id: int) -> None:
        with self._lock:
            self.active_profile_id = int(profile_id)

    def replace_scenes(self, scenes: list[dict]) -> None:
        with self._lock:
            self.scenes = list(scenes or [])

    def all_scenes(self) -> list[dict]:
        with self._lock:
            return list(self.scenes)

    def add_activity(self, *, kind: str, text: str,
                     dev_id: Optional[int] = None,
                     dev_name: str = "") -> None:
        with self._lock:
            self._activity.appendleft({
                "ts": time.time(),
                "kind": kind,
                "dev_id": dev_id,
                "dev_name": dev_name,
                "text": text,
            })

    def recent_activity(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._activity)[:limit]

    def merge_debug_messages(self, msgs: list[dict]) -> int:
        """Merge new debug messages, dedupe by id. Returns number actually added."""
        added = 0
        with self._lock:
            # Sort newest-first by id, then prepend any unseen
            for m in sorted(msgs or [], key=lambda x: int(x.get("id", 0)), reverse=True):
                mid = int(m.get("id", 0))
                if mid in self._debug_seen_ids:
                    continue
                self._debug_seen_ids.add(mid)
                self._debug_msgs.appendleft(m)
                added += 1
            # Trim seen set to ring buffer ids
            kept = {int(m.get("id", 0)) for m in self._debug_msgs}
            self._debug_seen_ids = kept
        return added

    def recent_debug_messages(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._debug_msgs)[:limit]

    def set_connected(self, ok: bool, error: str = "") -> bool:
        """Update connection state. Returns True if it changed."""
        with self._lock:
            changed = self._connected != ok
            self._connected = ok
            self._last_error = "" if ok else (error or self._last_error)
            return changed

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def attention_devices(self) -> list[dict]:
        """Devices needing attention: dead, or battery <= 20%."""
        out: list[dict] = []
        with self._lock:
            for d in self.devices.values():
                props = d.get("properties") or {}
                dead = bool(props.get("dead"))
                batt = props.get("batteryLevel")
                low_batt = False
                if batt is not None:
                    try:
                        low_batt = float(batt) <= 20
                    except (TypeError, ValueError):
                        pass
                if dead or low_batt:
                    out.append(d)
        return out

    def update_diagnostics(self, diag: dict) -> None:
        """Store new diagnostics sample; compute per-core busy% delta vs prev."""
        with self._lock:
            self.diagnostics = diag or {}
            cur = list(self.diagnostics.get("cpuLoad") or [])
            pcts: list[float] = []
            prev_by_name = {c.get("name"): c for c in self._prev_cpu}
            for c in cur:
                p = prev_by_name.get(c.get("name"))
                if p is None:
                    continue
                try:
                    du = int(c["user"]) - int(p["user"])
                    dn = int(c["nice"]) - int(p["nice"])
                    ds = int(c["system"]) - int(p["system"])
                    di = int(c["idle"]) - int(p["idle"])
                except (KeyError, ValueError, TypeError):
                    continue
                total = du + dn + ds + di
                if total <= 0:
                    continue
                pcts.append(100.0 * (du + dn + ds) / total)
            if pcts:
                self.cpu_pcts = pcts
            self._prev_cpu = cur

    def get_diagnostics(self) -> tuple[dict, list[float]]:
        with self._lock:
            return dict(self.diagnostics), list(self.cpu_pcts)

    def apply_change(self, change: dict) -> None:
        """Apply a single normalized change to the cache.

        Expected shape: {"id": int, "property": str, "newValue": Any, "oldValue": Any}
        """
        dev_id = change.get("id")
        prop = change.get("property") or change.get("name")
        if dev_id is None or not prop:
            return
        with self._lock:
            dev = self.devices.get(int(dev_id))
            if not dev:
                return
            dev.setdefault("properties", {})[prop] = change.get("newValue")

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._lock:
            return self.devices.get(int(device_id))

    def all_devices(self) -> list[dict]:
        with self._lock:
            return list(self.devices.values())

    def room_name(self, room_id: int) -> str:
        with self._lock:
            r = self.rooms.get(int(room_id))
            return r.get("name", "Unassigned") if r else "Unassigned"


class RefreshPoller:
    """Background thread looping HC3 /refreshStates and emitting change events."""

    def __init__(self, client: HC3Client, store: StateStore,
                 on_change: Callable[[dict], None],
                 poll_timeout_sec: int = 35,
                 error_backoff_sec: float = 5.0,
                 on_connection_change: Optional[Callable[[bool], None]] = None) -> None:
        self._client = client
        self._store = store
        self._on_change = on_change
        self._on_connection_change = on_connection_change
        self._poll_timeout = poll_timeout_sec
        self._error_backoff = error_backoff_sec
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_errors = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hc3-refresh-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        log.info("RefreshPoller started")
        while not self._stop.is_set():
            try:
                data = self._client.refresh_states(
                    last=self._store.last_refresh,
                    timeout=self._poll_timeout,
                )
                self._store.last_refresh = int(data.get("last", self._store.last_refresh))
                if self._consecutive_errors > 0 or not self._store.is_connected():
                    self._consecutive_errors = 0
                    if self._store.set_connected(True) and self._on_connection_change:
                        try: self._on_connection_change(True)
                        except Exception: log.exception("on_connection_change raised")

                # `changes` array (legacy/property snapshots)
                for change in data.get("changes", []) or []:
                    self._store.apply_change(change)
                    self._safe_emit(change)

                # `events` array — extract device property updates
                for event in data.get("events", []) or []:
                    etype = event.get("type")
                    edata = event.get("data") or {}
                    if etype == "DevicePropertyUpdatedEvent":
                        norm = {
                            "id": edata.get("id"),
                            "property": edata.get("property"),
                            "newValue": edata.get("newValue"),
                            "oldValue": edata.get("oldValue"),
                            "_event_type": etype,
                        }
                        self._store.apply_change(norm)
                        self._safe_emit(norm)
                    else:
                        # Forward other events with type so caller can log/ignore
                        self._safe_emit({"_event_type": etype, **edata})
            except HC3Error as e:
                log.warning("refreshStates error: %s", e)
                self._consecutive_errors += 1
                # Mark disconnected after 2 consecutive failures (one timeout is normal).
                if self._consecutive_errors >= 2:
                    if self._store.set_connected(False, str(e)) and self._on_connection_change:
                        try: self._on_connection_change(False)
                        except Exception: log.exception("on_connection_change raised")
                # Exponential backoff capped at 60s.
                backoff = min(self._error_backoff * (2 ** (self._consecutive_errors - 1)), 60.0)
                if self._stop.wait(backoff):
                    break
            except Exception:
                log.exception("Unexpected poller error")
                self._consecutive_errors += 1
                if self._consecutive_errors >= 2:
                    self._store.set_connected(False, "unexpected poller error")
                if self._stop.wait(self._error_backoff):
                    break
        log.info("RefreshPoller stopped")

    def _safe_emit(self, change: dict) -> None:
        try:
            self._on_change(change)
        except Exception:
            log.exception("on_change callback raised")


class UIEventQueue:
    """Bridge background thread → main thread (drained by a rumps.Timer)."""

    def __init__(self) -> None:
        self._q: queue.Queue[Any] = queue.Queue()

    def put(self, item: Any) -> None:
        self._q.put(item)

    def drain(self, max_items: int = 50) -> list[Any]:
        items = []
        for _ in range(max_items):
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items
