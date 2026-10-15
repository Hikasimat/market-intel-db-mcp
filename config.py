"""Environment configuration. Loads `.env` from this directory and fails readably."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

SERVER_DIR = Path(__file__).resolve().parent
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    """Raised when required settings are missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    username: str
    password: str = ""  # never logged / repr'd
    confirmation_ttl_seconds: int = 600
    request_timeout_seconds: float = 30.0
    log_level: str = "INFO"
    max_parent_lookup_pages: int = 10
    auth_token: str | None = None

    def __repr__(self) -> str:  # keep secrets out of any accidental log line
        return f"Settings(api_base_url={self.api_base_url!r}, username={self.username!r}, password='***')"


def _number(name: str, default: str, cast, minimum: float):
    raw = os.environ.get(name, default).strip()
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}.")
    return value


def load_settings(env_file: Path | None = None) -> Settings:
    """Load and validate settings. `env_file=None` uses `<server dir>/.env`."""
    load_dotenv(env_file or SERVER_DIR / ".env", override=False)

    missing = [
        n for n in ("MARKET_INTEL_API_BASE_URL", "MARKET_INTEL_USERNAME", "MARKET_INTEL_PASSWORD")
        if not os.environ.get(n, "").strip()
    ]
    if missing:
        raise ConfigError(
            "Missing required environment settings: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill them in."
        )

    base = os.environ["MARKET_INTEL_API_BASE_URL"].strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("MARKET_INTEL_API_BASE_URL must be an absolute http(s) URL ending in /api.")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise ConfigError("Use HTTPS for non-local hosts (plain http is allowed for localhost only).")

    return Settings(
        api_base_url=base,
        username=os.environ["MARKET_INTEL_USERNAME"].strip(),
        password=os.environ["MARKET_INTEL_PASSWORD"],
        confirmation_ttl_seconds=_number("MCP_CONFIRMATION_TTL_SECONDS", "600", int, 30),
        request_timeout_seconds=_number("MCP_REQUEST_TIMEOUT_SECONDS", "30", float, 1),
        log_level=os.environ.get("MCP_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        auth_token=os.environ.get("MCP_AUTH_TOKEN", "").strip() or None,
    )
