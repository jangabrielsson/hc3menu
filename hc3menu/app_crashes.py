"""App crash reporter: catches uncaught **Python** exceptions inside the
HC3 Menu app itself (i.e. bugs in *our* code), writes a structured entry to
``~/.hc3menu/crash.log`` and posts a one-time macOS notification per process
session so the user knows the app hit an internal error.

This is *not* about HC3-side issues like QA errors (those are surfaced via
the QA-error notifications) or QuickApp restarts (handled separately by
``PluginProcessCrashedEvent`` in ``app.py``). It only fires when a Python
exception escapes one of our worker threads or the main thread.

We deliberately keep this lightweight (no third-party deps): a plain rolling
text log capped at ~256 KB, with the most recent entry on top. The user can
attach this file to bug reports.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import rumps

from .config import CONFIG_DIR

log = logging.getLogger(__name__)

CRASH_LOG: Path = CONFIG_DIR / "crash.log"
_MAX_LOG_BYTES = 256 * 1024  # ~256 KB rolling cap.

_lock = threading.Lock()
_notified = False  # one notification per process session
_installed = False


def _format_entry(where: str, exc_type, exc, tb) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    tb_text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
    header = f"=== {when} [{where}] {exc_type.__name__}: {exc} ==="
    return f"{header}\n{tb_text}\n\n"


def _write(entry: str) -> None:
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        # Prepend so newest entry is on top; then truncate from the bottom.
        existing = ""
        if CRASH_LOG.exists():
            try:
                existing = CRASH_LOG.read_text(errors="replace")
            except OSError:
                existing = ""
        combined = entry + existing
        if len(combined) > _MAX_LOG_BYTES:
            combined = combined[:_MAX_LOG_BYTES]
        CRASH_LOG.write_text(combined)
    except Exception:
        # Last-resort: never let the crash reporter itself crash anything.
        log.exception("crash reporter failed to write log")


def _notify_once() -> None:
    global _notified
    with _lock:
        if _notified:
            return
        _notified = True
    try:
        rumps.notification(
            title="HC3 Menu hit an error",
            subtitle="A crash report was saved",
            message="See ~/.hc3menu/crash.log for details.",
        )
    except Exception:
        log.debug("failed to post crash notification", exc_info=True)


def report(where: str, exc_type, exc, tb) -> None:
    """Public hook: record an exception manually (e.g. from a wrapped
    worker callable). Safe to call from any thread."""
    if exc_type is None or exc is None:
        return
    # Always log to stderr/log first so devs see it during `python -m hc3menu`.
    log.error("Crash in %s: %s", where, exc, exc_info=(exc_type, exc, tb))
    with _lock:
        _write(_format_entry(where, exc_type, exc, tb))
    _notify_once()


def _sys_excepthook(exc_type, exc, tb) -> None:
    report("main-thread", exc_type, exc, tb)


def _thread_excepthook(args) -> None:  # threading.ExceptHookArgs
    # Skip SystemExit raised by normal interpreter shutdown.
    if args.exc_type is SystemExit:
        return
    where = f"thread:{args.thread.name if args.thread else '?'}"
    report(where, args.exc_type, args.exc_value, args.exc_traceback)


def install() -> None:
    """Install global excepthooks. Call once at app startup."""
    global _installed
    if _installed:
        return
    _installed = True
    sys.excepthook = _sys_excepthook
    try:
        threading.excepthook = _thread_excepthook  # type: ignore[attr-defined]
    except AttributeError:
        # Python < 3.8 fallback: not relevant for us (we require 3.10+).
        pass


def wrap(where: str):
    """Decorator factory: wraps a callable so any exception is reported but
    not re-raised. Useful for ThreadPoolExecutor.submit() callables that
    would otherwise swallow the traceback into a Future no-one awaits."""
    def deco(fn):
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                report(where, *sys.exc_info())
                return None
        wrapped.__name__ = getattr(fn, "__name__", "wrapped")
        return wrapped
    return deco


def reset_session_state() -> None:
    """For tests: forget that we already notified this session."""
    global _notified
    _notified = False


def crash_log_path() -> Optional[Path]:
    """Return the crash log path if it exists and has content."""
    if CRASH_LOG.exists() and CRASH_LOG.stat().st_size > 0:
        return CRASH_LOG
    return None
