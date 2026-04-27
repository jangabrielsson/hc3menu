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
        self.notifier = Notifier(
            self.store, self.config.notifications,
            attention_enabled=self.config.attention_notifications,
            low_battery_threshold=self.config.low_battery_threshold,
        )
        self._action_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hc3-action")
        self._lock = threading.Lock()
        self._prefs_ctrl = None  # PreferencesController; created lazily
        self._debug_ctrl = None  # DebugLogController; created lazily
        self._search_ctrl = None  # SearchController; created lazily
        # Per-QA-tag throttle: tag -> last notify monotonic timestamp.
        self._qa_error_last_notify: dict[str, float] = {}

        self.dispatcher = MenuActionDispatcher(self._submit_action)

        self._build_initial_menu()

        # Drain UI queue periodically and rebuild menu when changes arrive
        self._ui_timer = rumps.Timer(self._tick_ui, 1.0)
        self._ui_timer.start()

        # Periodic diagnostics fetch (HC3 doesn't push these via refreshStates).
        self._diag_timer = rumps.Timer(self._tick_diag, 10.0)
        self._diag_timer.start()

        # Periodic auto-update check (cheap: only fires once per
        # `auto_update_interval_sec` and only when the user opted in).
        self._update_timer = rumps.Timer(self._tick_auto_update, 3600.0)
        self._update_timer.start()
        # Also kick one shortly after launch so the first check happens
        # without waiting an hour.
        self._launch_update_timer = rumps.Timer(self._launch_auto_update, 30.0)
        self._launch_update_timer.start()

        # Global hotkey (lazy import — keeps Carbon/AppKit cost out of tests).
        self._hotkey = None  # GlobalHotkey, created on demand
        self._apply_global_hotkey_config()

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
        # Seed attention dedupe with currently-bad devices so we don't fire
        # a notification storm on launch / reconnect.
        self._seed_attention_state()
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
        try:
            self.store.replace_favorite_colors(self.client.get_favorite_colors())
        except HC3Error as e:
            log.warning("Could not fetch favorite colors: %s", e)
            self.store.replace_favorite_colors([])

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
            on_about=self._on_about,
            on_show_debug_log=self._open_debug_log,
            notification_toggles=[
                ("Attention (battery / dead)",
                 self.config.attention_notifications,
                 self._toggle_attention_notifications),
                ("QA errors",
                 self.config.qa_error_notifications,
                 self._toggle_qa_error_notifications),
                ("QA crashes",
                 self.config.qa_crash_notifications,
                 self._toggle_qa_crash_notifications),
            ],
            on_show_crash_log=(
                self._show_crash_log
                if self._has_crash_log() else None
            ),
            auto_update_toggle=(
                self.config.auto_update_check,
                self._toggle_auto_update_check,
            ),
            global_hotkey_toggle=(
                self.config.global_hotkey_enabled,
                self.config.global_hotkey,
                self._toggle_global_hotkey,
            ),
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
            try:
                fav_colors = self.client.get_favorite_colors()
            except HC3Error as e:
                log.warning("Could not refresh favorite colors: %s", e)
                fav_colors = self.store.all_favorite_colors()
            self.store.replace_devices(devices)
            self.store.replace_rooms(rooms)
            self.store.replace_partitions(parts)
            self.store.replace_profiles(profiles)
            self.store.replace_scenes(scenes)
            self.store.replace_favorite_colors(fav_colors)
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

    # -- Search ----------------------------------------------------------
    def _open_search(self) -> None:
        from .search_window import SearchController
        if getattr(self, "_search_ctrl", None) is None:
            self._search_ctrl = SearchController(
                self.store,
                on_run_scene=self._on_scene_click,
                on_toggle_device=self._on_search_toggle_device,
                on_shutter_toggle=self._on_search_shutter_toggle,
                creds=self.creds,
            )
        else:
            # Keep creds fresh in case the user reconfigured.
            self._search_ctrl.creds = self.creds
        self._search_ctrl.show()

    def _on_search_toggle_device(self, dev_id: int) -> None:
        """Toggle a switch/dimmer/color device based on its current value."""
        if self.client is None:
            return
        d = self.store.get_device(int(dev_id)) or {}
        actions = d.get("actions") or {}
        if "turnOn" not in actions and "turnOff" not in actions:
            log.info("search: device %s has no turnOn/turnOff action; ignoring",
                     dev_id)
            return
        props = d.get("properties") or {}
        on = bool(props.get("value"))
        action = "turnOff" if on else "turnOn"
        self.dispatcher.submit(lambda: self.client.call_action(int(dev_id), action))

    def _on_search_shutter_toggle(self, dev_id: int) -> None:
        """For shutters: open if mostly closed, otherwise close."""
        if self.client is None:
            return
        d = self.store.get_device(int(dev_id)) or {}
        props = d.get("properties") or {}
        try:
            v = float(props.get("value") or 0)
        except (TypeError, ValueError):
            v = 0.0
        action = "open" if v < 50 else "close"
        self.dispatcher.submit(lambda: self.client.call_action(int(dev_id), action))

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

    # -- Attention notifications ----------------------------------------
    def _seed_attention_state(self) -> None:
        """Pre-populate notifier dedupe sets with currently-bad devices so
        we don't fire a storm on launch / reconnect."""
        threshold = self.config.low_battery_threshold
        for d in self.store.attention_devices():
            try:
                dev_id = int(d["id"])
            except (KeyError, TypeError, ValueError):
                continue
            props = d.get("properties") or {}
            if bool(props.get("dead")):
                self.notifier._dead_notified.add(dev_id)
            batt = props.get("batteryLevel")
            if batt is not None:
                try:
                    if float(batt) <= threshold:
                        self.notifier._low_batt_notified.add(dev_id)
                except (TypeError, ValueError):
                    pass

    def _toggle_attention_notifications(self) -> None:
        self.config.attention_notifications = not self.config.attention_notifications
        self.notifier.configure_attention(
            enabled=self.config.attention_notifications,
            low_battery_threshold=self.config.low_battery_threshold,
        )
        if self.config.attention_notifications:
            # Re-seed so we don't immediately notify about pre-existing issues.
            self.notifier.reset_attention_state()
            self._seed_attention_state()
        try:
            save_config(self.config)
        except Exception:
            log.exception("failed to persist attention_notifications")
        self.ui_queue.put(("rebuild", None))

    def _toggle_qa_error_notifications(self) -> None:
        self.config.qa_error_notifications = not self.config.qa_error_notifications
        # Reset throttle so re-enabling immediately works.
        self._qa_error_last_notify.clear()
        try:
            save_config(self.config)
        except Exception:
            log.exception("failed to persist qa_error_notifications")
        self.ui_queue.put(("rebuild", None))

    def _toggle_qa_crash_notifications(self) -> None:
        self.config.qa_crash_notifications = not self.config.qa_crash_notifications
        try:
            save_config(self.config)
        except Exception:
            log.exception("failed to persist qa_crash_notifications")
        self.ui_queue.put(("rebuild", None))

    def _handle_plugin_crash(self, change: dict) -> None:
        """HC3 emits PluginProcessCrashedEvent when a QA's Lua process dies
        and is auto-restarted. The payload typically carries `deviceId` (the
        QA id) and an optional reason. We surface a notification so the user
        sees their QA misbehaved even if they weren't watching the menu."""
        log.warning("plugin process crashed: %r", change)
        dev_id = change.get("deviceId") or change.get("id")
        reason = change.get("error") or change.get("reason") or ""
        dev_name = "?"
        if dev_id is not None:
            try:
                d = self.store.get_device(int(dev_id)) or {}
                dev_name = d.get("name") or f"QA {dev_id}"
            except (TypeError, ValueError):
                dev_name = f"QA {dev_id}"
        self.store.add_activity(
            kind="device",
            dev_id=int(dev_id) if dev_id is not None else None,
            dev_name=dev_name,
            text=f"{dev_name}: process crashed (auto-restarted)",
        )
        if self.config.qa_crash_notifications:
            try:
                rumps.notification(
                    title=f"QA crashed — {dev_name}",
                    subtitle="HC3 restarted the plugin",
                    message=str(reason)[:200] if reason else "",
                )
            except Exception:
                log.debug("notification failed", exc_info=True)
        self.ui_queue.put(("rebuild", None))

    # -- Crash log ------------------------------------------------------
    def _has_crash_log(self) -> bool:
        from . import app_crashes
        return app_crashes.crash_log_path() is not None

    def _show_crash_log(self) -> None:
        """Reveal ~/.hc3menu/crash.log in Finder."""
        from . import app_crashes
        import subprocess
        path = app_crashes.crash_log_path()
        if path is None:
            return
        try:
            subprocess.run(["open", "-R", str(path)], check=False)
        except Exception:
            log.exception("failed to open crash log in Finder")

    # -- Debug log window ----------------------------------------------
    def _open_debug_log(self) -> None:
        """Open the live HC3 debug log window (lazy import of PyObjC)."""
        from .debug_window import DebugLogController
        if getattr(self, "_debug_ctrl", None) is None:
            self._debug_ctrl = DebugLogController(
                self.store, self.creds, self.client
            )
        else:
            # Refresh credentials/client in case they changed.
            self._debug_ctrl.creds = self.creds
            self._debug_ctrl.client = self.client
        self._debug_ctrl.show()

    # -- About ----------------------------------------------------------
    def _on_about(self) -> None:
        """Show an About dialog with version + project info."""
        import webbrowser
        try:
            from AppKit import NSApplication
            _app = NSApplication.sharedApplication()
            _prev_policy = _app.activationPolicy()
            _app.setActivationPolicy_(0)
            _app.activateIgnoringOtherApps_(True)
        except Exception:
            _app = None
            _prev_policy = None
        try:
            hc3 = ""
            try:
                from .config import load_credentials
                creds = load_credentials()
                if creds.host:
                    hc3 = f"\nHC3: {creds.base_url()}"
            except Exception:
                pass
            resp = rumps.alert(
                title="HC3 Menu",
                message=(
                    f"Version {__version__}\n"
                    "A macOS menu bar app for Fibaro Home Center 3.\n"
                    "© 2026 Jan Gabrielsson"
                    f"{hc3}"
                ),
                ok="OK",
                other="Project page",
            )
        finally:
            if _app is not None and _prev_policy is not None:
                try:
                    _app.setActivationPolicy_(_prev_policy)
                except Exception:
                    pass
        if resp == -1:
            webbrowser.open("https://github.com/jangabrielsson/hc3menu")

    # -- Update check ---------------------------------------------------
    def _on_check_updates(self) -> None:
        """Background fetch of the latest GitHub release; show alert."""
        def work():
            info = updater.check_for_update()
            self.ui_queue.put(("update_result", info))
        self._action_pool.submit(work)

    # -- Auto-update --------------------------------------------------
    def _toggle_auto_update_check(self) -> None:
        self.config.auto_update_check = not self.config.auto_update_check
        try:
            save_config(self.config)
        except Exception:
            log.exception("failed to persist auto_update_check")
        self.ui_queue.put(("rebuild", None))
        # If user just turned it on, kick a check ~immediately.
        if self.config.auto_update_check:
            self._launch_update_timer = rumps.Timer(self._launch_auto_update, 5.0)
            self._launch_update_timer.start()

    # -- Global hotkey ------------------------------------------------
    def _apply_global_hotkey_config(self) -> None:
        """(Re)install or uninstall the global hotkey to match config."""
        try:
            from . import global_hotkey as gh_mod
        except Exception:
            log.exception("global_hotkey: import failed")
            return
        if self._hotkey is None:
            self._hotkey = gh_mod.GlobalHotkey(self._on_global_hotkey)
        if not self.config.global_hotkey_enabled:
            self._hotkey.uninstall()
            return
        chord = gh_mod.parse_chord(self.config.global_hotkey or "")
        if chord is None:
            log.warning("global_hotkey: cannot parse %r; disabling",
                        self.config.global_hotkey)
            return
        mods, key = chord
        if not self._hotkey.install(mods, key):
            log.warning("global_hotkey: install failed (Accessibility permission?)")

    def _toggle_global_hotkey(self) -> None:
        self.config.global_hotkey_enabled = not self.config.global_hotkey_enabled
        try:
            save_config(self.config)
        except Exception:
            log.exception("failed to persist global_hotkey_enabled")
        self._apply_global_hotkey_config()
        self.ui_queue.put(("rebuild", None))
        if self.config.global_hotkey_enabled and (
                self._hotkey is None or not self._hotkey.is_installed):
            # Likely missing Accessibility permission — surface a hint.
            try:
                rumps.notification(
                    "Global hotkey not active",
                    "Grant Accessibility permission",
                    "System Settings → Privacy & Security → Accessibility, "
                    "then enable HC3 Menu.",
                )
            except Exception:
                pass

    def _on_global_hotkey(self) -> None:
        """Fired on the AppKit main thread when the user hits the chord.

        Programmatically clicks the menubar icon so the menu drops down.
        """
        try:
            item = getattr(self, "nsstatusitem", None)
            if item is None:
                return
            btn = item.button() if hasattr(item, "button") else None
            if btn is not None:
                btn.performClick_(None)
        except Exception:
            log.exception("global_hotkey: failed to open menu")

    def _launch_auto_update(self, sender) -> None:
        """One-shot: fires ~30s after launch (or 5s after enabling), then
        cancels itself. Real periodic checks come from _tick_auto_update."""
        try:
            sender.stop()
        except Exception:
            pass
        self._tick_auto_update(sender)

    def _tick_auto_update(self, _sender) -> None:
        if not self.config.auto_update_check:
            return
        import time as _t
        now = _t.time()
        interval = max(3600, int(self.config.auto_update_interval_sec or 86400))
        if now - float(self.config.auto_update_last_check or 0.0) < interval:
            return

        def work():
            info = updater.check_for_update()
            try:
                self.config.auto_update_last_check = _t.time()
                save_config(self.config)
            except Exception:
                log.debug("could not persist auto_update_last_check",
                          exc_info=True)
            # Only surface UI when there's actually something newer; on
            # background polls we silently swallow "up to date" / errors so
            # we don't spam the user.
            if info is not None and info.is_newer:
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
            # Force the app to the foreground so the alert isn't hidden
            # behind other windows. Because we're an LSUIElement / Accessory
            # app, we have to temporarily promote to a regular activation
            # policy for the alert to actually come to front, then revert.
            try:
                from AppKit import NSApplication
                _app = NSApplication.sharedApplication()
                _prev_policy = _app.activationPolicy()
                # NSApplicationActivationPolicyRegular = 0
                _app.setActivationPolicy_(0)
                _app.activateIgnoringOtherApps_(True)
            except Exception:
                _app = None
                _prev_policy = None
            try:
                resp = rumps.alert(
                    title=f"Update available: v{info.latest}",
                    message=(f"You have v{info.current}.\n\n"
                             + (info.notes[:400] if info.notes else "")),
                    ok="Open download page",
                    cancel="Later",
                )
            finally:
                # Revert to Accessory (no Dock icon) after the alert closes.
                if _app is not None and _prev_policy is not None:
                    try:
                        _app.setActivationPolicy_(_prev_policy)
                    except Exception:
                        pass
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
                # Fetch all severities so the Debug log window can filter
                # locally (trace/debug/info as well as warning/error). The
                # notification path below only triggers on `type == error`.
                resp = self.client.get_debug_messages(offset=100)
                msgs = resp.get("messages") or []
                # Detect new errors (only after first poll has happened).
                already_initialized = bool(self.store.recent_debug_messages(1))
                added = self.store.merge_debug_messages(msgs)
                if added and already_initialized:
                    new_errors = [m for m in self.store.recent_debug_messages(added)
                                  if m.get("type") == "error"]
                    if self.config.qa_error_notifications:
                        for m in new_errors[:3]:  # cap to 3 notifications/cycle
                            self._notify_qa_error(m)
            except HC3Error as e:
                log.debug("debug messages fetch failed: %s", e)
            self.ui_queue.put(("rebuild", None))
        self._action_pool.submit(work)

    def _notify_qa_error(self, msg: dict) -> None:
        try:
            tag = msg.get("tag") or "QA"
            # Per-tag throttle: if we already notified about this QA recently,
            # skip so a chatty QA doesn't spam Notification Center.
            import time
            now = time.monotonic()
            last = self._qa_error_last_notify.get(tag, 0.0)
            if now - last < max(1, self.config.qa_error_throttle_sec):
                return
            self._qa_error_last_notify[tag] = now
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

        # QuickApp / scene plugin process crashed (HC3 will auto-restart it).
        if etype == "PluginProcessCrashedEvent":
            self._handle_plugin_crash(change)
            return

        dev_id = change.get("id")
        prop = change.get("property") or change.get("name")
        new_v = change.get("newValue")
        old_v = change.get("oldValue")
        dev = self.store.get_device(int(dev_id)) if dev_id is not None else None
        dev_name = (dev or {}).get("name", "?")

        if etype == "DevicePropertyUpdatedEvent" and prop:
            # Attention notifications (battery / dead) — must run before the
            # _RELEVANT_PROPS filter, since `dead` and `batteryLevel` are
            # not displayed in the menu but we still want to notify.
            try:
                self.notifier.handle_attention(change)
            except Exception:
                log.exception("attention notifier failed")
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
                # Log + crash report (won't re-raise).
                from . import app_crashes
                import sys
                app_crashes.report("action-pool", *sys.exc_info())
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
            # Re-apply global hotkey settings (chord may have changed).
            self._apply_global_hotkey_config()
            # Rebuild menu so the hotkey label and toggle states reflect changes.
            self.ui_queue.put(("rebuild", None))
            self._start_session()

        if self._prefs_ctrl is None:
            self._prefs_ctrl = PreferencesController(
                self.creds, self.config, devices, on_save
            )
        else:
            # Refresh data on the existing controller so re-opening picks up
            # any changes made via the menu since last open.
            self._prefs_ctrl.creds = self.creds
            self._prefs_ctrl.config = self.config
            self._prefs_ctrl.devices = devices
            self._prefs_ctrl.on_save = on_save
        self._prefs_ctrl.show()


def main() -> None:
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Quiet down noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # Install crash reporter (catches uncaught exceptions in main + worker
    # threads, writes ~/.hc3menu/crash.log, posts a one-time notification).
    from . import app_crashes
    app_crashes.install()
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
