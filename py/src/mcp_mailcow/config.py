"""Configuration loaded from environment variables.

Each mode has its own required variables. Missing required vars exit with a
clear error rather than failing at first tool call.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

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
# all mode (admin API + per-mailbox IMAP/SMTP via temporary app passwords)
# =============================================================================

#: Default lifetime (seconds) of a cached temporary app password.
DEFAULT_TEMP_PASSWORD_TTL = 600
_MIN_TEMP_PASSWORD_TTL = 30
_MAX_TEMP_PASSWORD_TTL = 86400

# Deliberately strict: the address is also used in an admin API URL path
# (/api/v1/get/app-passwd/all/<mailbox>), so no '/', '?', '#', '%' or spaces.
_MAILBOX_RE = re.compile(r"^[a-z0-9._+\-]+@[a-z0-9](?:[a-z0-9.\-]*[a-z0-9])?$")


def normalize_mailbox(value: object) -> str | None:
    """Lower-cased, stripped mailbox address, or None if it is not a plain
    ``local@domain`` address."""
    if not isinstance(value, str):
        return None
    mb = value.strip().lower()
    return mb if _MAILBOX_RE.match(mb) else None


@dataclass(frozen=True)
class MailboxAllowList:
    """Parsed MCP_MAILCOW_MAILBOXES. ``allow_all`` is set by a ``*`` entry."""

    mailboxes: frozenset[str] = frozenset()
    allow_all: bool = False

    def allows(self, mailbox: str) -> bool:
        return self.allow_all or mailbox in self.mailboxes


def parse_mailbox_allowlist(raw: str) -> MailboxAllowList:
    """Parse a comma-separated allow-list (case-insensitive, blanks ignored).

    Raises ValueError for an empty list or an entry that is not ``*`` or a
    plain mailbox address. The message never includes the offending value.
    """
    entries = [p.strip() for p in raw.split(",") if p.strip()]
    if not entries:
        raise ValueError("is empty")
    allow_all = False
    mailboxes: set[str] = set()
    for i, entry in enumerate(entries, start=1):
        if entry == "*":
            allow_all = True
            continue
        mb = normalize_mailbox(entry)
        if mb is None:
            raise ValueError(f"entry #{i} is not '*' or a mailbox address (local@domain)")
        mailboxes.add(mb)
    return MailboxAllowList(mailboxes=frozenset(mailboxes), allow_all=allow_all)


@dataclass(frozen=True)
class AllConfig:
    """Config for all mode: the admin API plus IMAP/SMTP access to the
    allowed mailboxes through short-lived app passwords.

    No mailbox password is configured; MAILCOW_MAIL_* are not read.
    """

    admin: AdminConfig
    allowlist: MailboxAllowList
    host: str
    imap_host: str
    smtp_host: str
    imap_port: int = 993
    smtp_port: int = 587
    tls_verify: bool = True
    temp_password_ttl: float = DEFAULT_TEMP_PASSWORD_TTL
    enable_mail_send: bool = False
    allow_sensitive_tools: bool = False
    audit_log: Path = DEFAULT_AUDIT_LOG

    def user_config(self, mailbox: str, password: str) -> UserConfig:
        """UserConfig for one mailbox, reusing the user-mode IMAP/SMTP code."""
        return UserConfig(
            host=self.host,
            mail_user=mailbox,
            mail_pass=password,
            imap_host=self.imap_host,
            smtp_host=self.smtp_host,
            imap_port=self.imap_port,
            smtp_port=self.smtp_port,
            tls_verify=self.tls_verify,
            audit_log=self.audit_log,
        )


def _int_env(var: str, default: int, lo: int, hi: int) -> int:
    raw = (os.environ.get(var) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        val = lo - 1
    if not lo <= val <= hi:
        _fail(f"{var} must be an integer between {lo} and {hi}")
    return val


def load_all_config() -> AllConfig:
    """Load all-mode config. Exits (code 2) naming — never printing — a
    missing or invalid variable."""
    admin = load_admin_config()
    raw = _require("MCP_MAILCOW_MAILBOXES")
    try:
        allowlist = parse_mailbox_allowlist(raw)
    except ValueError as e:
        _fail(f"MCP_MAILCOW_MAILBOXES {e}")
    host = (os.environ.get("MAILCOW_HOST") or "").strip() or (
        urlsplit(admin.base_url).hostname or ""
    )
    if not host:
        _fail("MAILCOW_HOST is not set and no host could be taken from MAILCOW_ADMIN_URL")
    return AllConfig(
        admin=admin,
        allowlist=allowlist,
        host=host,
        imap_host=os.environ.get("MAILCOW_IMAP_HOST") or host,
        smtp_host=os.environ.get("MAILCOW_SMTP_HOST") or host,
        imap_port=_int_env("MAILCOW_IMAP_PORT", 993, 1, _MAX_PORT),
        smtp_port=_int_env("MAILCOW_SMTP_PORT", 587, 1, _MAX_PORT),
        tls_verify=_bool_env("MCP_MAILCOW_TLS_VERIFY", True),
        temp_password_ttl=_int_env(
            "MCP_MAILCOW_TEMP_PASSWORD_TTL",
            DEFAULT_TEMP_PASSWORD_TTL,
            _MIN_TEMP_PASSWORD_TTL,
            _MAX_TEMP_PASSWORD_TTL,
        ),
        enable_mail_send=_bool_env("MCP_MAILCOW_ENABLE_MAIL_SEND", False),
        allow_sensitive_tools=_bool_env("MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS", False),
        audit_log=admin.audit_log,
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


MODES = ("user", "admin", "all")


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


MIN_TOKEN_LENGTH = 24
# The value shipped in .env.example. Copying that file unedited must not start a server.
PLACEHOLDER_TOKEN = "replace-with-a-long-random-token"


def _check_token_strength(token: str) -> None:
    """Exit (code 2) if ``token`` is the example placeholder or too short.

    The messages state the reason only; the token value is never included.
    """
    if token == PLACEHOLDER_TOKEN:
        _fail(
            "MCP_MAILCOW_AUTH_TOKEN is still the placeholder value from .env.example. "
            "Refusing to start; set a real random token, e.g. "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if len(token) < MIN_TOKEN_LENGTH:
        _fail(
            f"MCP_MAILCOW_AUTH_TOKEN is too short (minimum {MIN_TOKEN_LENGTH} characters). "
            "Refusing to start; generate one with "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )


def load_http_config(host: str, port: int) -> HttpConfig:
    """Load HTTP-only settings. Exits (code 2) when no auth token is configured
    and MCP_MAILCOW_ALLOW_NO_AUTH is not explicitly true, or when a configured
    token is the .env.example placeholder or shorter than MIN_TOKEN_LENGTH.
    A configured token is always validated, even if ALLOW_NO_AUTH is true."""
    token = (os.environ.get("MCP_MAILCOW_AUTH_TOKEN") or "").strip() or None
    if token is not None:
        _check_token_strength(token)
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


def write_tools_enabled(transport: str, mode: str | None = None) -> bool:
    """``MCP_MAILCOW_ENABLE_WRITE_TOOLS``; defaults to true for stdio (unchanged
    behaviour) and false for HTTP (a network-reachable server is read-only
    unless explicitly opted in). In ``all`` mode the default is false for
    every transport: that server reads untrusted mail from many mailboxes
    while holding the admin API key."""
    return _bool_env("MCP_MAILCOW_ENABLE_WRITE_TOOLS", transport == "stdio" and mode != "all")
