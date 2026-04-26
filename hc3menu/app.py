"""Main rumps app: wires together client, state, poller, menu, prefs, notifications."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import rumps

from .config import AppConfig, HC3Credentials, load_config, load_credentials, save_config
from .__version__ import __version__
from . import updater
from .hc3_client import HC3Client, HC3Error
from .menu_builder import MenuActionDispatcher, build_root_menu
from .notifications import Notifier
from .state import RefreshPoller, StateStore, UIEventQueue

log = logging.getLogger(__name__)


class HC3MenuApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("HC3", title="HC3", quit_button=rumps.MenuItem("Quit"))
        self.creds: HC3Credentials = load_credentials()
        self.config: AppConfig = load_config()
        self.store = StateStore()
        self.client: HC3Client | None = None
        self.poller: RefreshPoller | None = None
        self.ui_queue = UIEventQueue()
        self.notifier = Notifier(self.store, self.config.notifications)
        self._action_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hc3-action")
        self._lock = threading.Lock()
        self._prefs_ctrl = None  # PreferencesController; created lazily

        self.dispatcher = MenuActionDispatcher(self._submit_action)

        self._build_initial_menu()

        # Drain UI queue periodically and rebuild menu when changes arrive
        self._ui_timer = rumps.Timer(self._tick_ui, 1.0)
        self._ui_timer.start()

        # Periodic diagnostics fetch (HC3 doesn't push these via refreshStates).
        self._diag_timer = rumps.Timer(self._tick_diag, 10.0)
        self._diag_timer.start()

        if self.creds.is_complete():
            self._start_session()
        else:
            rumps.alert(
                "HC3 Menu — first run",
                "No HC3 credentials configured. Open Preferences… to set them.",
            )
            self.open_prefs(None)

    # -- Session lifecycle ------------------------------------------------
    def _start_session(self) -> None:
        self._stop_session()
        try:
            self.client = HC3Client(self.creds)
            devices = self.client.get_devices()
            rooms = self.client.get_rooms()
        except HC3Error as e:
            rumps.alert("HC3 connection failed", str(e))
            return

        self.store.replace_devices(devices)
        self.store.replace_rooms(rooms)
        try:
            self.store.replace_partitions(self.client.get_partitions())
        except HC3Error as e:
            log.warning("Could not fetch alarm partitions: %s", e)
            self.store.replace_partitions([])
        try:
            self.store.replace_profiles(self.client.get_profiles())
        except HC3Error as e:
            log.warning("Could not fetch profiles: %s", e)
            self.store.replace_profiles({})
        try:
            self.store.replace_scenes(self.client.get_scenes())
        except HC3Error as e:
            log.warning("Could not fetch scenes: %s", e)
            self.store.replace_scenes([])

        self.poller = RefreshPoller(
            self.client, self.store,
            on_change=self._on_change_bg,
            poll_timeout_sec=self.config.poll_timeout_sec,
            on_connection_change=self._on_connection_change,
        )
        self.poller.start()
        self.store.set_connected(True)
        self._rebuild_menu()

    def _stop_session(self) -> None:
        if self.poller is not None:
            self.poller.stop()
            self.poller = None
        self.client = None

    # -- Menu management --------------------------------------------------
    def _build_initial_menu(self) -> None:
        self.menu.clear()
        loading = rumps.MenuItem("Loading…")
        loading.set_callback(None)
        self.menu = [loading, None, rumps.MenuItem("Preferences…", callback=self.open_prefs)]

    def _rebuild_menu(self) -> None:
        if self.client is None:
            return
        self.menu.clear()
        items = build_root_menu(
            self.store, self.config.favorites,
            self.dispatcher, self.client,
            on_refresh=self._refresh_now,
            on_prefs=lambda: self.open_prefs(None),
            partitions=self.store.all_partitions(),
            on_arm=self._on_arm_click,
            on_disarm=self._on_disarm_click,
            on_arm_all=self._on_arm_all_click,
            on_disarm_all=self._on_disarm_all_click,
            profiles=self.store.all_profiles(),
            on_profile=self._on_profile_click,
            scenes=self.store.all_scenes(),
            on_scene=self._on_scene_click,
            diagnostics=self.store.get_diagnostics(),
            activity=self.store.recent_activity(20),
            attention=self.store.attention_devices(),
            debug_messages=self.store.recent_debug_messages(20),
            on_favorite_toggle=self._on_favorite_toggle,
            on_check_updates=self._on_check_updates,
            version=__version__,
        )
        self.menu = items
        self._update_status_icon()

    def _refresh_now(self) -> None:
        if self.client is None:
            return

        def work():
            try:
                devices = self.client.get_devices()
                rooms = self.client.get_rooms()
                parts = self.client.get_partitions()
                profiles = self.client.get_profiles()
                scenes = self.client.get_scenes()
            except HC3Error as e:
                log.warning("Manual refresh failed: %s", e)
                return
            self.store.replace_devices(devices)
            self.store.replace_rooms(rooms)
            self.store.replace_partitions(parts)
            self.store.replace_profiles(profiles)
            self.store.replace_scenes(scenes)
            self.ui_queue.put(("rebuild", None))

        self._action_pool.submit(work)

    # -- Alarm click handlers -------------------------------------------
    # Show optimistic "Arming…" / "Disarming…" while HC3 runs its
    # entry/exit delay. Cleared when the matching AlarmPartitionArmedEvent
    # arrives, or after a 90s safety timeout.
    _PENDING_TIMEOUT = 90.0

    def _begin_pending(self, pid: int, action: str) -> None:
        field = "_pending_arm" if action == "arm" else "_pending_disarm"
        other = "_pending_disarm" if action == "arm" else "_pending_arm"
        self.store.update_partition(pid, **{field: True, other: False})
        self.ui_queue.put(("rebuild", None))

        def safety_clear():
            import time
            time.sleep(self._PENDING_TIMEOUT)
            for p in self.store.all_partitions():
                if int(p.get("id", -1)) == pid and p.get(field):
                    log.warning("pending %s on partition %s timed out", action, pid)
                    self.store.update_partition(pid, **{field: False})
                    self.ui_queue.put(("rebuild", None))
                    return
        threading.Thread(target=safety_clear, daemon=True).start()

    def _on_arm_click(self, pid: int) -> None:
        self._begin_pending(pid, "arm")
        self.dispatcher.submit(lambda: self.client.arm_partition(pid))

    def _on_disarm_click(self, pid: int) -> None:
        self._begin_pending(pid, "disarm")
        self.dispatcher.submit(lambda: self.client.disarm_partition(pid))

    def _on_arm_all_click(self) -> None:
        for p in self.store.all_partitions():
            self._begin_pending(int(p["id"]), "arm")
        self.dispatcher.submit(self.client.arm_all_partitions)

    def _on_disarm_all_click(self) -> None:
        for p in self.store.all_partitions():
            self._begin_pending(int(p["id"]), "disarm")
        self.dispatcher.submit(self.client.disarm_all_partitions)

    # -- Profile click handler ------------------------------------------
    def _on_profile_click(self, profile_id: int) -> None:
        # Optimistic local update; HC3 confirms via ActiveProfileChangedEvent.
        self.store.set_active_profile(profile_id)
        self.ui_queue.put(("rebuild", None))
        self.dispatcher.submit(lambda: self.client.set_active_profile(profile_id))

    # -- Scene click handler --------------------------------------------
    def _on_scene_click(self, scene_id: int) -> None:
        self.dispatcher.submit(lambda: self.client.run_scene(scene_id))

    # -- Favorite toggle ------------------------------------------------
    def _on_favorite_toggle(self, dev_id: int) -> None:
        favs = [int(x) for x in self.config.favorites]
        if dev_id in favs:
            favs.remove(dev_id)
        else:
            favs.append(dev_id)
        self.config.favorites = favs
        try:
            save_config(self.config)
        except Exception:
            log.exception("failed to persist favorites")
        self.ui_queue.put(("rebuild", None))

    # -- Update check ---------------------------------------------------
    def _on_check_updates(self) -> None:
        """Background fetch of the latest GitHub release; show alert."""
        def work():
            info = updater.check_for_update()
            self.ui_queue.put(("update_result", info))
        self._action_pool.submit(work)

    def _show_update_result(self, info) -> None:
        import webbrowser
        if info is None:
            try:
                rumps.notification(
                    title="Update check failed",
                    subtitle="",
                    message="Could not reach GitHub. Check your connection.",
                )
            except Exception:
                pass
            return
        if not info.is_newer:
            try:
                rumps.notification(
                    title="HC3 Menu is up to date",
                    subtitle=f"v{info.current}",
                    message="",
                )
            except Exception:
                pass
            return
        # New version available — modal alert with two buttons.
        try:
            resp = rumps.alert(
                title=f"Update available: v{info.latest}",
                message=(f"You have v{info.current}.\n\n"
                         + (info.notes[:400] if info.notes else "")),
                ok="Open download page",
                cancel="Later",
            )
            if resp == 1:
                webbrowser.open(info.download_url or info.html_url)
        except Exception:
            log.exception("update alert failed")

    # -- Status bar icon -------------------------------------------------
    def _update_status_icon(self) -> None:
        """Reflect alarm/breach/connection state in the menu bar icon."""
        from .sf_symbols import sf_image
        try:
            from AppKit import NSColor
        except ImportError:
            return
        connected = self.store.is_connected()
        parts = self.store.all_partitions()
        breached = any(p.get("breached") for p in parts)
        pending = any(p.get("_pending_arm") or p.get("_pending_disarm") for p in parts)
        armed_count = sum(1 for p in parts if p.get("armed"))
        if not connected:
            sym, color = "house.slash.fill", NSColor.systemGrayColor()
        elif breached:
            sym, color = "house.fill", NSColor.systemRedColor()
        elif pending:
            sym, color = "house.fill", NSColor.systemYellowColor()
        elif parts and armed_count == len(parts):
            sym, color = "house.fill", NSColor.systemRedColor()
        elif armed_count > 0:
            sym, color = "house.fill", NSColor.systemOrangeColor()
        else:
            sym, color = "house.fill", None  # template (auto light/dark)
        img = sf_image(sym, color=color)
        if img is None:
            return
        nsapp = getattr(self, "_nsapp", None)
        if nsapp is None:
            # rumps hasn't started its NSApp yet (during __init__ rebuild); skip.
            return
        try:
            nsapp.nsstatusitem.setImage_(img)
            nsapp.nsstatusitem.setTitle_("")
        except Exception:
            log.debug("could not set status item image", exc_info=True)

    # -- Diagnostics tick ------------------------------------------------
    def _tick_diag(self, _) -> None:
        if self.client is None:
            return
        def work():
            try:
                diag = self.client.get_diagnostics()
            except HC3Error as e:
                log.debug("diagnostics fetch failed: %s", e)
                diag = None
            if diag is not None:
                self.store.update_diagnostics(diag)
            try:
                resp = self.client.get_debug_messages(
                    types=["warning", "error"], offset=20)
                msgs = resp.get("messages") or []
                # Detect new errors (only after first poll has happened).
                already_initialized = bool(self.store.recent_debug_messages(1))
                added = self.store.merge_debug_messages(msgs)
                if added and already_initialized:
                    new_errors = [m for m in self.store.recent_debug_messages(added)
                                  if m.get("type") == "error"]
                    for m in new_errors[:3]:  # cap to 3 notifications/cycle
                        self._notify_qa_error(m)
            except HC3Error as e:
                log.debug("debug messages fetch failed: %s", e)
            self.ui_queue.put(("rebuild", None))
        self._action_pool.submit(work)

    def _notify_qa_error(self, msg: dict) -> None:
        try:
            tag = msg.get("tag") or "QA"
            text = (msg.get("message") or "").strip().replace("\n", " ")
            if len(text) > 200:
                text = text[:197] + "…"
            rumps.notification(
                title=f"HC3 error — {tag}",
                subtitle="",
                message=text or "(no message)",
            )
        except Exception:
            log.debug("notification failed", exc_info=True)

    # -- Connection state ------------------------------------------------
    def _on_connection_change(self, connected: bool) -> None:
        log.info("connection state -> %s", "connected" if connected else "disconnected")
        try:
            if connected:
                rumps.notification(title="HC3 reconnected", subtitle="", message="")
                self.store.add_activity(kind="alarm", text="HC3 reconnected")
            else:
                rumps.notification(
                    title="HC3 disconnected",
                    subtitle="",
                    message=self.store.last_error() or "",
                )
                self.store.add_activity(kind="breach", text="HC3 disconnected")
        except Exception:
            log.debug("notification failed", exc_info=True)
        self.ui_queue.put(("rebuild", None))
    # -- Background callbacks --------------------------------------------
    # Properties whose changes affect what the menu displays.
    _RELEVANT_PROPS = {
        "value",                          # switches, dimmers (level), shutters (% open), sensors
        "state",                          # dimmers/switches on-off
        "color",                          # color controllers (R,G,B,W string)
        "colorComponents",                # color controllers (per-channel dict)
        "heatingThermostatSetpoint",      # thermostat
        "coolingThermostatSetpoint",      # thermostat
        "targetLevel",                    # legacy thermostat
    }

    def _on_change_bg(self, change: dict) -> None:
        # Runs in poller thread.
        etype = change.get("_event_type", "DevicePropertyUpdatedEvent")

        # Alarm partition events: optimistically update state, queue rebuild,
        # then refetch authoritative state in background.
        if etype and etype.startswith("AlarmPartition"):
            log.info("alarm event %s: %r", etype, change)
            pid = change.get("partitionId") or change.get("id")
            if pid is not None:
                if "armed" in change:
                    self.store.update_partition(int(pid), armed=bool(change["armed"]))
                    # Clear matching pending flag now that HC3 transitioned.
                    if bool(change["armed"]):
                        self.store.update_partition(int(pid), _pending_arm=False)
                        self.store.add_activity(kind="alarm", text=f"Partition {pid} armed")
                    else:
                        self.store.update_partition(int(pid), _pending_disarm=False)
                        self.store.add_activity(kind="alarm", text=f"Partition {pid} disarmed")
                if "breached" in change:
                    self.store.update_partition(int(pid), breached=bool(change["breached"]))
                    if bool(change["breached"]):
                        self.store.add_activity(kind="breach", text=f"Partition {pid} BREACHED")
                        try:
                            rumps.notification(
                                title="⚠️ Alarm BREACHED",
                                subtitle=f"Partition {pid}",
                                message="",
                            )
                        except Exception:
                            log.debug("notification failed", exc_info=True)
            # Queue immediate rebuild from optimistic state.
            self.ui_queue.put(("rebuild", None))
            if self.client is not None:
                def refetch():
                    try:
                        parts = self.client.get_partitions()
                    except HC3Error as e:
                        log.warning("Refetch partitions failed: %s", e)
                        return
                    log.debug("refetched %d partitions after %s", len(parts), etype)
                    self.store.replace_partitions(parts)
                    self.ui_queue.put(("rebuild", None))
                self._action_pool.submit(refetch)
            return

        # Active profile changes.
        if etype == "ActiveProfileChangedEvent":
            new_id = change.get("newActiveProfile") or change.get("activeProfile") \
                or change.get("newValue") or change.get("id")
            log.info("active profile changed -> %s", new_id)
            if new_id is not None:
                self.store.set_active_profile(int(new_id))
                # Try to look up name for nicer activity entry
                pname = None
                for p in self.store.all_profiles()[0]:
                    if int(p.get("id", -1)) == int(new_id):
                        pname = p.get("name")
                        break
                self.store.add_activity(
                    kind="profile",
                    text=f"Profile -> {pname or new_id}")
                self.ui_queue.put(("rebuild", None))
            return

        dev_id = change.get("id")
        prop = change.get("property") or change.get("name")
        new_v = change.get("newValue")
        old_v = change.get("oldValue")
        dev = self.store.get_device(int(dev_id)) if dev_id is not None else None
        dev_name = (dev or {}).get("name", "?")

        if etype == "DevicePropertyUpdatedEvent" and prop:
            if prop not in self._RELEVANT_PROPS:
                log.debug("event %s: id=%s name=%r prop=%s (not displayed, skipping rebuild)",
                          etype, dev_id, dev_name, prop)
                return
            log.info("event %s: id=%s name=%r prop=%s %r -> %r",
                     etype, dev_id, dev_name, prop, old_v, new_v)
            try:
                self.notifier.handle_change(change)
            except Exception:
                log.exception("notifier failed")
            self.store.add_activity(
                kind="device",
                dev_id=int(dev_id) if dev_id is not None else None,
                dev_name=dev_name,
                text=f"{dev_name}: {prop} = {new_v}",
            )
            self.ui_queue.put(("change", change))
        else:
            log.debug("event %s ignored: %r", etype, change)

    def _submit_action(self, fn) -> None:
        def wrapped():
            try:
                fn()
            except HC3Error as e:
                log.warning("Action failed: %s", e)
            except Exception:
                log.exception("Action raised")
        self._action_pool.submit(wrapped)

    def _tick_ui(self, _) -> None:
        items = self.ui_queue.drain()
        if not items:
            return
        # Handle update-result events independently from menu rebuilds.
        for kind, payload in items:
            if kind == "update_result":
                self._show_update_result(payload)
        # Coalesce: any change → single rebuild
        change_count = sum(1 for kind, _ in items if kind == "change")
        rebuild_req = any(kind == "rebuild" for kind, _ in items)
        if change_count or rebuild_req:
            log.info("ui tick: rebuilding menu (changes=%d, manual_refresh=%s)",
                     change_count, rebuild_req)
            try:
                self._rebuild_menu()
            except Exception:
                log.exception("menu rebuild failed")

    # -- Preferences ------------------------------------------------------
    def open_prefs(self, _) -> None:
        # Lazy import: PyObjC import only needed when window is opened
        from .prefs_window import PreferencesController

        devices = self.store.all_devices()
        if not devices and self.creds.is_complete():
            try:
                tmp = HC3Client(self.creds, request_timeout=5)
                devices = tmp.get_devices()
                self.store.replace_devices(devices)
            except HC3Error as e:
                log.warning("Could not fetch devices for prefs: %s", e)

        def on_save(creds: HC3Credentials, cfg: AppConfig) -> None:
            self.creds = creds
            self.config = cfg
            self.notifier.set_rules(cfg.notifications)
            self._start_session()

        self._prefs_ctrl = PreferencesController(
            self.creds, self.config, devices, on_save
        )
        self._prefs_ctrl.show()


def main() -> None:
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Hide from the Dock when running from source (the packaged .app uses
    # LSUIElement=True in its Info.plist; this covers `python -m hc3menu`).
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().setActivationPolicy_(2)  # Accessory
    except Exception:
        pass
    HC3MenuApp().run()


if __name__ == "__main__":
    main()
