"""Audit log for MCP tool invocations.

Writes JSONL entries to ~/.local/state/mcp-mailcow/audit.log (XDG-compliant)
with one line per tool call. Secrets are masked automatically.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .secret_sanitize import sanitize_text

# Param names whose values are masked in the audit log.
SECRET_PARAM_NAMES = frozenset({
    "password",
    "password2",
    "app_passwd",
    "app_passwd2",
    "client_secret",
    "api_key",
    "key",
    "token",
})


def _mask_secrets(params: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for k, v in params.items():
        if k in SECRET_PARAM_NAMES and v:
            masked[k] = "***"
        elif isinstance(v, dict):
            masked[k] = _mask_secrets(v)
        else:
            masked[k] = v
    return masked


# --- all mode: metadata-only params ---------------------------------------------
#
# In all mode the audit log records *which* mailbox was touched and how, never
# the mail itself. User-tool params are reduced to this allow-list; recipient
# lists become counts. Anything else (subject, body, body_html, body_prefix,
# attachments, search query, attachment part ids, ...) is dropped.
_USER_META_PARAMS = frozenset({
    "uid",
    "folder",
    "source_folder",
    "dest_folder",
    "name",
    "old_name",
    "new_name",
    "limit",
    "unread_only",
    "since",
    "mark_read",
    "fetch_html",
    "reply_all",
    "include_quoted",
    "include_attachments",
    "purge",
    "confirm",
    "flag",
    "value",
    "max_size_mb",
})
_RECIPIENT_PARAMS = ("to", "cc", "bcc")
# Mail content that may appear in admin-tool params (send_test_mail).
_CONTENT_PARAMS = frozenset({
    "subject", "body", "body_html", "body_prefix", "attachments", "query",
})


def user_metadata_params(params: dict[str, Any]) -> dict[str, Any]:
    """Metadata-only view of a user-tool call (all mode)."""
    out = {k: v for k, v in params.items() if k in _USER_META_PARAMS}
    for k in _RECIPIENT_PARAMS:
        v = params.get(k)
        if v:
            out[f"{k}_count"] = len(v) if isinstance(v, (list, tuple)) else 1
    return out


def drop_content_params(params: dict[str, Any]) -> dict[str, Any]:
    """Admin-tool params without mail content (all mode)."""
    return {k: v for k, v in params.items() if k not in _CONTENT_PARAMS}


class AuditLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        action: str,
        params: dict[str, Any],
        result: str,
        duration_ms: int,
        error: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
            "action": action,
            "params": _mask_secrets(params),
            "result": result,
            "duration_ms": duration_ms,
        }
        if error:
            entry["error"] = sanitize_text(error)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @contextmanager
    def trace(self, action: str, params: dict[str, Any]) -> Iterator[None]:
        """Context manager that auto-logs success or error with duration."""
        start = time.perf_counter()
        try:
            yield
            self.write(
                action=action,
                params=params,
                result="ok",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        except Exception as e:
            self.write(
                action=action,
                params=params,
                result="err",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error=sanitize_text(str(e)),
            )
            raise


class ScopedAudit(AuditLogger):
    """An AuditLogger writing to ``base``'s file that adds fixed fields
    (``mode``, ``kind``, ``mailbox``) to every entry and filters the params
    first. Used by all mode; handlers see an ordinary AuditLogger."""

    def __init__(
        self,
        base: AuditLogger,
        extra: dict[str, Any],
        params_filter: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.path = base.path  # base already created the directory
        self._base = base
        self._extra = dict(extra)
        self._filter = params_filter

    def write(
        self,
        action: str,
        params: dict[str, Any],
        result: str,
        duration_ms: int,
        error: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._base.write(
            action=action,
            params=self._filter(params) if self._filter else params,
            result=result,
            duration_ms=duration_ms,
            error=error,
            extra={**self._extra, **(extra or {})},
        )
