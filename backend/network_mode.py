"""Local and trusted-LAN network settings for the backend process."""

from __future__ import annotations

import os
import ipaddress
from collections.abc import Mapping
from urllib.parse import urlsplit


DEFAULT_BACKEND_BIND_HOST = "127.0.0.1"
LOCAL_FRONTEND_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
BACKEND_BIND_HOST_ENV = "NOVEL_G_BACKEND_HOST"
EXTRA_CORS_ORIGINS_ENV = "NOVEL_G_CORS_ORIGINS"


def get_backend_bind_host(environment: Mapping[str, str] | None = None) -> str:
    """Return the explicit backend bind host, defaulting to IPv4 loopback."""
    env = os.environ if environment is None else environment
    return env.get(BACKEND_BIND_HOST_ENV, "").strip() or DEFAULT_BACKEND_BIND_HOST


def is_loopback_host(value: str | None) -> bool:
    if not value:
        return False
    if value.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.strip()).is_loopback
    except ValueError:
        return False


def is_lan_access_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether the backend is explicitly bound beyond loopback."""
    return not is_loopback_host(get_backend_bind_host(environment))


def _normalize_origin(value: str) -> str:
    origin = value.strip().rstrip("/")
    if not origin or origin == "*":
        raise ValueError("CORS origin must be an explicit http(s) origin")

    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Invalid CORS origin: {value!r}")

    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid CORS origin: {value!r}") from exc
    return origin


def get_cors_origins(environment: Mapping[str, str] | None = None) -> list[str]:
    """Build a credential-safe exact CORS allowlist from process settings."""
    env = os.environ if environment is None else environment
    configured = env.get(EXTRA_CORS_ORIGINS_ENV, "")
    origins = list(LOCAL_FRONTEND_ORIGINS)
    for raw_origin in configured.split(","):
        if not raw_origin.strip():
            continue
        origin = _normalize_origin(raw_origin)
        if origin not in origins:
            origins.append(origin)
    return origins
