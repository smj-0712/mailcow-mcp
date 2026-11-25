"""All mode: admin tools and user tools in one server.

Admin tools call the Mailcow REST API exactly as in admin mode. User tools
take an extra required ``mailbox`` argument; for each call the mailbox is
validated against MCP_MAILCOW_MAILBOXES, a temporary app password for it is
leased from :class:`TempAppPasswordManager`, and the unchanged user-mode
handler runs with a per-call ``UserContext`` for that mailbox.

The user-mode handlers do blocking IMAP/SMTP I/O inside ``async def``. In
all mode one server serves many mailboxes (and the HTTP transport serves
concurrent requests), so each user handler is run in a worker thread with
its own short event loop instead of on the server's loop.

Audit entries carry ``mode: "all"``, ``kind`` (``admin``/``user``) and, for
user tools, ``mailbox``; user-tool params are reduced to metadata (UIDs,
folders, recipient counts), never subject/body/attachments.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from .admin_tools import AdminContext
from .audit import AuditLogger, ScopedAudit, drop_content_params, user_metadata_params
from .config import AllConfig, normalize_mailbox
from .exceptions import AccessDenied
from .registry import ToolHandler, build_admin_registry, user_tool_factories
from .schema import MAILBOX_ARG
from .temp_app_passwords import TempAppPasswordManager
from .user_tools import UserContext

MODE_FIELDS_ADMIN = {"mode": "all", "kind": "admin"}


def admin_audit(base: AuditLogger) -> AuditLogger:
    return ScopedAudit(base, MODE_FIELDS_ADMIN, drop_content_params)


def user_audit(base: AuditLogger, mailbox: str | None) -> AuditLogger:
    return ScopedAudit(
        base, {"mode": "all", "kind": "user", "mailbox": mailbox}, user_metadata_params
    )


def audit_for_call(
    base: AuditLogger, tool_def: dict[str, Any], arguments: dict[str, Any]
) -> AuditLogger:
    """Audit logger for a refusal recorded by the dispatcher (all mode)."""
    if tool_def.get("mode") == "user":
        return user_audit(base, normalize_mailbox(arguments.get(MAILBOX_ARG)))
    return admin_audit(base)


@dataclass
class AllContext:
    config: AllConfig
    audit: AuditLogger
    admin: AdminContext
    passwords: TempAppPasswordManager

    async def astart(self) -> None:
        """Remove leftover temp app passwords, then start the TTL sweeper."""
        await self.passwords.cleanup_leftovers(self.config.allowlist)
        self.passwords.start_sweeper()

    async def aclose(self) -> None:
        try:
            await self.passwords.aclose()
        finally:
            await self.admin.aclose()


def _run_in_thread(handler: ToolHandler, args: dict[str, Any]) -> Any:
    coro: Coroutine[Any, Any, Any] = handler(args)  # type: ignore[assignment]
    return asyncio.run(coro)


def _user_tool(
    ctx: AllContext, name: str, factory: Callable[[UserContext], ToolHandler]
) -> ToolHandler:
    async def h(args: dict[str, Any]) -> Any:
        mailbox = normalize_mailbox(args.get(MAILBOX_ARG))
        rest = {k: v for k, v in args.items() if k != MAILBOX_ARG}
        audit = user_audit(ctx.audit, mailbox)
        if mailbox is None:
            audit.write(name, rest, "denied", 0, error="missing or invalid mailbox argument")
            raise AccessDenied(
                f"'{MAILBOX_ARG}' must be a mailbox address (local@domain)"
            )
        if not ctx.config.allowlist.allows(mailbox):
            audit.write(name, rest, "denied", 0, error="mailbox not allowed")
            raise AccessDenied(
                f"mailbox '{mailbox}' is not allowed on this server (MCP_MAILCOW_MAILBOXES)"
            )
        async with ctx.passwords.lease(mailbox) as lease:
            user_ctx = UserContext(
                config=ctx.config.user_config(lease.username, lease.password), audit=audit
            )
            return await asyncio.to_thread(_run_in_thread, factory(user_ctx), rest)

    return h


def build_all_registry(
    config: AllConfig, audit: AuditLogger
) -> tuple[dict[str, ToolHandler], AllContext]:
    registry, admin_ctx = build_admin_registry(config.admin, admin_audit(audit))
    ctx = AllContext(
        config=config,
        audit=audit,
        admin=admin_ctx,
        passwords=TempAppPasswordManager(
            admin_ctx.client, admin_audit(audit), ttl=config.temp_password_ttl
        ),
    )
    for name, factory in user_tool_factories().items():
        if name in registry:  # pragma: no cover - names are unique (test_schema)
            raise RuntimeError(f"tool name clash in all mode: {name}")
        registry[name] = _user_tool(ctx, name, factory)
    return registry, ctx
