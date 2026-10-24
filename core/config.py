"""Read runtime settings from the environment."""

import os

from core.errors import ToolFailure


class ConfigError(ToolFailure):
    """A required environment variable is missing or invalid."""


def require_env(name: str) -> str:
    """Return one environment variable, or raise when it is blank."""
    value = os.environ.get(name, "").strip()
    if value == "":
        raise ConfigError(name + " is not set.")
    return value


def lock_minutes() -> int:
    """How long a stock lock lasts. Defaults to 120 minutes."""
    raw = os.environ.get("LOCK_MINUTES", "").strip()
    if raw == "":
        return 120
    try:
        minutes = int(raw)
    except ValueError as exc:
        raise ConfigError("LOCK_MINUTES must be a whole number of minutes.") from exc
    if minutes <= 0:
        raise ConfigError("LOCK_MINUTES must be greater than zero.")
    return minutes


def brand_name() -> str:
    """The single candle brand named on Paystack payments."""
    return require_env("BRAND_NAME")


def listen_host() -> str:
    """Bind address. Local default is 127.0.0.1. Hosts like Render need 0.0.0.0."""
    host = os.environ.get("HOST", "").strip()
    if host != "":
        return host
    # Render and similar platforms inject PORT; bind all interfaces there.
    if os.environ.get("PORT", "").strip() != "" or os.environ.get("RENDER", "").strip() != "":
        return "0.0.0.0"
    return "127.0.0.1"


def listen_port() -> int:
    """HTTP port. Uses PORT when the host sets it, otherwise 8000."""
    raw = os.environ.get("PORT", "").strip()
    if raw == "":
        return 8000
    try:
        port = int(raw)
    except ValueError as exc:
        raise ConfigError("PORT must be a whole number.") from exc
    if port <= 0 or port > 65535:
        raise ConfigError("PORT must be between 1 and 65535.")
    return port


def public_base_url() -> str:
    """Public HTTPS origin for auth metadata and docs.

    Prefer PUBLIC_BASE_URL. On Render, RENDER_EXTERNAL_URL is filled in automatically.
    Local default is http://127.0.0.1:8000.
    """
    for name in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        value = os.environ.get(name, "").strip().rstrip("/")
        if value != "":
            return value
    return "http://127.0.0.1:" + str(listen_port())
