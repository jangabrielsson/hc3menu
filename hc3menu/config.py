"""Configuration loading: .env (creds) + ~/.hc3menu/config.json (favorites, rules)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, set_key

CONFIG_DIR = Path.home() / ".hc3menu"
CONFIG_FILE = CONFIG_DIR / "config.json"
ENV_FILE = CONFIG_DIR / ".env"


@dataclass
class HC3Credentials:
    host: str = ""
    port: int = 80
    https: bool = False
    user: str = ""
    password: str = ""
    pin: str = ""  # 4-digit alarm PIN (optional)

    @property
    def base_url(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}:{self.port}/api"

    def is_complete(self) -> bool:
        return bool(self.host and self.user and self.password)


@dataclass
class NotificationRule:
    device_id: int = 0
    property: str = "value"
    condition: str = "any"  # 'any' | 'true' | 'false' | '>X' | '<X' | '==X'
    message: str = "{name} {property} -> {newValue}"


@dataclass
class AppConfig:
    favorites: list[int] = field(default_factory=list)
    notifications: list[NotificationRule] = field(default_factory=list)
    poll_timeout_sec: int = 35


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_credentials() -> HC3Credentials:
    """Load credentials from ~/.hc3menu/.env, falling back to project .env or env vars."""
    _ensure_dirs()
    # Try config-dir .env first, then cwd .env
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)
    load_dotenv(override=False)

    return HC3Credentials(
        host=os.getenv("HC3_HOST", ""),
        port=int(os.getenv("HC3_PORT", "80") or "80"),
        https=(os.getenv("HC3_HTTPS", "false").lower() in ("1", "true", "yes")),
        user=os.getenv("HC3_USER", ""),
        password=os.getenv("HC3_PASSWORD", ""),
        pin=os.getenv("HC3_PIN", ""),
    )


def save_credentials(creds: HC3Credentials) -> None:
    _ensure_dirs()
    if not ENV_FILE.exists():
        ENV_FILE.touch()
        ENV_FILE.chmod(0o600)
    set_key(str(ENV_FILE), "HC3_HOST", creds.host)
    set_key(str(ENV_FILE), "HC3_PORT", str(creds.port))
    set_key(str(ENV_FILE), "HC3_HTTPS", "true" if creds.https else "false")
    set_key(str(ENV_FILE), "HC3_USER", creds.user)
    set_key(str(ENV_FILE), "HC3_PASSWORD", creds.password)
    set_key(str(ENV_FILE), "HC3_PIN", creds.pin)
    # Also update process env so reload sees fresh values
    os.environ["HC3_HOST"] = creds.host
    os.environ["HC3_PORT"] = str(creds.port)
    os.environ["HC3_HTTPS"] = "true" if creds.https else "false"
    os.environ["HC3_USER"] = creds.user
    os.environ["HC3_PASSWORD"] = creds.password
    os.environ["HC3_PIN"] = creds.pin


def load_config() -> AppConfig:
    _ensure_dirs()
    if not CONFIG_FILE.exists():
        return AppConfig()
    try:
        data: dict[str, Any] = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    rules = [NotificationRule(**r) for r in data.get("notifications", []) if isinstance(r, dict)]
    return AppConfig(
        favorites=[int(x) for x in data.get("favorites", [])],
        notifications=rules,
        poll_timeout_sec=int(data.get("poll_timeout_sec", 35)),
    )


def save_config(cfg: AppConfig) -> None:
    _ensure_dirs()
    payload = {
        "favorites": cfg.favorites,
        "notifications": [asdict(r) for r in cfg.notifications],
        "poll_timeout_sec": cfg.poll_timeout_sec,
    }
    CONFIG_FILE.write_text(json.dumps(payload, indent=2))
