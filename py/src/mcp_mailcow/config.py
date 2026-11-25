"""Configuration loaded from environment variables.

Each mode has its own required variables. Missing required vars exit with a
clear error rather than failing at first tool call.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from platformdirs import user_state_dir

DEFAULT_AUDIT_LOG = Path(user_state_dir("mcp-mailcow")) / "audit.log"


@dataclass(frozen=True)
class UserConfig:
    """Config for user mode (IMAP/SMTP).

    `imap_host` and `smtp_host` default to `host` when not set explicitly.
    Override them only if your IMAP and SMTP submission run on different
    servers (rare — e.g. external SMTP relay in front of local IMAP).

    `mail_pass` is excluded from __repr__ so it cannot leak into an
    exception traceback, a debug print, or a log line that interpolates
    the config object. The value is still readable via direct attribute
    access from within the module (which is what the IMAP/SMTP helpers do).
    """

    host: str
    mail_user: str
    mail_pass: str = field(repr=False)
    imap_host: str = ""  # falls back to `host` in load_user_config
    smtp_host: str = ""  # falls back to `host` in load_user_config
    imap_port: int = 993
    smtp_port: int = 587
    tls_verify: bool = True
    audit_log: Path = DEFAULT_AUDIT_LOG


@dataclass(frozen=True)
class AdminConfig:
    """Config for admin mode (Mailcow REST API).

    `api_timeout` controls how long a single HTTP request can hang before
    httpx aborts. Default 60s (was 30s in v0.3 — bumped because Mailcow
    list endpoints can be slow on busy instances). Override via
    ``MCP_MAILCOW_API_TIMEOUT`` env var.

    `api_key` is excluded from __repr__ (see UserConfig.mail_pass).
    """

    base_url: str
    api_key: str = field(repr=False)
    tls_verify: bool = True
    api_timeout: float = 60.0
    audit_log: Path = DEFAULT_AUDIT_LOG


def _require(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        sys.stderr.write(f"error: required env var {var} is not set\n")
        sys.exit(2)
    return val


def _bool_env(var: str, default: bool) -> bool:
    val = os.environ.get(var)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _path_env(var: str, default: Path) -> Path:
    val = os.environ.get(var)
    return Path(val).expanduser() if val else default


def load_user_config() -> UserConfig:
    host = _require("MAILCOW_HOST")
    return UserConfig(
        host=host,
        mail_user=_require("MAILCOW_MAIL_USER"),
        mail_pass=_require("MAILCOW_MAIL_PASS"),
        imap_host=os.environ.get("MAILCOW_IMAP_HOST") or host,
        smtp_host=os.environ.get("MAILCOW_SMTP_HOST") or host,
        imap_port=int(os.environ.get("MAILCOW_IMAP_PORT", "993")),
        smtp_port=int(os.environ.get("MAILCOW_SMTP_PORT", "587")),
        tls_verify=_bool_env("MCP_MAILCOW_TLS_VERIFY", True),
        audit_log=_path_env("MCP_MAILCOW_AUDIT_LOG", DEFAULT_AUDIT_LOG),
    )


def load_admin_config() -> AdminConfig:
    return AdminConfig(
        base_url=_require("MAILCOW_ADMIN_URL").rstrip("/"),
        api_key=_require("MAILCOW_ADMIN_API_KEY"),
        tls_verify=_bool_env("MCP_MAILCOW_TLS_VERIFY", True),
        api_timeout=float(os.environ.get("MCP_MAILCOW_API_TIMEOUT", "60")),
        audit_log=_path_env("MCP_MAILCOW_AUDIT_LOG", DEFAULT_AUDIT_LOG),
    )


# =============================================================================
# Transport (stdio / HTTP)
# =============================================================================

TRANSPORTS = ("stdio", "http")
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HTTP_HOST = "0.0.0.0"  # container/panel deployments bind all interfaces
DEFAULT_HTTP_PORT = 8000
_MAX_PORT = 65535


def _fail(msg: str) -> NoReturn:
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(2)


MODES = ("user", "admin")


def env_mode() -> str | None:
    """Default for ``--mode`` (``MCP_MAILCOW_MODE``); None when unset/empty."""
    val = (os.environ.get("MCP_MAILCOW_MODE") or "").strip().lower()
    if not val:
        return None
    if val not in MODES:
        _fail(f"MCP_MAILCOW_MODE must be one of {', '.join(MODES)}")
    return val


def env_transport() -> str:
    """Default for ``--transport`` (``MCP_MAILCOW_TRANSPORT``, else stdio)."""
    val = (os.environ.get("MCP_MAILCOW_TRANSPORT") or DEFAULT_TRANSPORT).strip().lower()
    if val not in TRANSPORTS:
        _fail(f"MCP_MAILCOW_TRANSPORT must be one of {', '.join(TRANSPORTS)}")
    return val


def env_host() -> str:
    """Default for ``--host`` (``MCP_MAILCOW_HOST``, else 0.0.0.0)."""
    return (os.environ.get("MCP_MAILCOW_HOST") or DEFAULT_HTTP_HOST).strip()


def env_port() -> int:
    """Default for ``--port``: ``MCP_MAILCOW_PORT``, else ``SERVER_PORT`` (set by
    Pterodactyl Wings to the server's primary allocation), else 8000."""
    for var in ("MCP_MAILCOW_PORT", "SERVER_PORT"):
        raw = (os.environ.get(var) or "").strip()
        if raw:
            break
    else:
        return DEFAULT_HTTP_PORT
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if not 0 < port <= _MAX_PORT:
        _fail(f"{var} must be an integer between 1 and 65535")
    return port


def _csv_env(var: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(var, "").split(",") if p.strip())


@dataclass(frozen=True)
class HttpConfig:
    """Settings for the HTTP transport.

    ``auth_token`` is excluded from __repr__ so it can never leak through a
    log line or traceback that formats the config object.
    """

    host: str
    port: int
    auth_token: str | None = field(default=None, repr=False)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()


def load_http_config(host: str, port: int) -> HttpConfig:
    """Load HTTP-only settings. Exits (code 2) when no auth token is configured
    and MCP_MAILCOW_ALLOW_NO_AUTH is not explicitly true."""
    token = (os.environ.get("MCP_MAILCOW_AUTH_TOKEN") or "").strip() or None
    if token is None and not _bool_env("MCP_MAILCOW_ALLOW_NO_AUTH", False):
        _fail(
            "HTTP transport requires MCP_MAILCOW_AUTH_TOKEN (clients must send "
            "'Authorization: Bearer <token>'). Refusing to start an unauthenticated "
            "server. Set MCP_MAILCOW_ALLOW_NO_AUTH=true only if access is already "
            "restricted by other means (private network, authenticating proxy)."
        )
    if not 0 < port <= _MAX_PORT:
        _fail("--port must be between 1 and 65535")
    return HttpConfig(
        host=host,
        port=port,
        auth_token=token,
        allowed_hosts=_csv_env("MCP_MAILCOW_ALLOWED_HOSTS"),
        allowed_origins=_csv_env("MCP_MAILCOW_ALLOWED_ORIGINS"),
    )


def write_tools_enabled(transport: str) -> bool:
    """``MCP_MAILCOW_ENABLE_WRITE_TOOLS``; defaults to true for stdio (unchanged
    behaviour) and false for HTTP (a network-reachable server is read-only
    unless explicitly opted in)."""
    return _bool_env("MCP_MAILCOW_ENABLE_WRITE_TOOLS", transport == "stdio")
