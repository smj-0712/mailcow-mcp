"""Short-lived Mailcow app passwords for all mode.

All mode reads and sends mail for many mailboxes without knowing their
passwords. For each mailbox it creates, through the admin API, an *app
password* (an additional login credential; the mailbox password is never
read or changed) limited to IMAP and SMTP, uses it for IMAP/SMTP logins, and
deletes it again.

Lifecycle
---------
* **Create** on first use of a mailbox: ``POST /api/v1/add/app-passwd``. The
  response carries no id, so the id is then looked up by the (unique) app
  name with ``GET /api/v1/get/app-passwd/all/<mailbox>`` (the item field is
  ``name``).
* **Cache** per mailbox for ``ttl`` seconds so not every tool call creates
  one. Calls hold a *lease*; an expired credential that is still leased is
  deleted when its last lease ends.
* **Delete** (``POST /api/v1/delete/app-passwd`` with ``[id]``) when the TTL
  expires (background sweeper, or the next lease), and for all of them at
  server shutdown (:meth:`aclose`).
* **Startup cleanup** (:meth:`cleanup_leftovers`): delete app passwords whose
  name starts with :data:`TEMP_PREFIX` in the allowed mailboxes — leftovers of
  a crash. App passwords with any other name are never touched.

Secrets: the plaintext password lives only in memory (in a ``repr=False``
field) and is registered with :mod:`secret_sanitize` while it exists, so it
is masked in any sanitized error/log text. It is never written to the audit
log; audit entries carry the mailbox and app name only.

One asyncio.Lock per mailbox serialises create/delete so concurrent calls for
the same mailbox never create two app passwords.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from .audit import AuditLogger
from .config import MailboxAllowList, normalize_mailbox
from .mailcow_api import MailcowClient
from .secret_sanitize import register_secret, sanitize_exception, unregister_secret

logger = logging.getLogger("mcp_mailcow.temp_app_passwords")

#: Name prefix of every app password this server creates. Only app passwords
#: with this prefix are ever deleted by the startup cleanup.
TEMP_PREFIX = "mcp-temp-"
#: Protocols granted to the temporary app password (Mailcow ``*_access``).
TEMP_PROTOCOLS = ("imap_access", "smtp_access")

_PASSWORD_BODY_BYTES = 32  # token_urlsafe(32) → 43 chars, 256 bits


def generate_password() -> str:
    """A long random password that also has one character of each class, in
    case the Mailcow password policy requires them. URL-safe characters
    only, so it needs no quoting in IMAP LOGIN or SMTP AUTH."""
    return (
        secrets.token_urlsafe(_PASSWORD_BODY_BYTES)
        + secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("-_.")
    )


def generate_app_name() -> str:
    return f"{TEMP_PREFIX}{int(time.time())}-{secrets.token_hex(6)}"


class TempPasswordError(RuntimeError):
    """Creating/looking up a temporary app password failed."""


@dataclass
class _Credential:
    mailbox: str
    app_name: str
    app_id: int
    password: str = field(repr=False)
    expires_at: float
    leases: int = 0


@dataclass(frozen=True)
class Lease:
    """What a tool call gets: the login name and the password."""

    username: str
    password: str = field(repr=False)


def _items(data: Any) -> list[dict[str, Any]]:
    """Mailcow returns ``{}`` (not ``[]``) for a mailbox without app passwords."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


class TempAppPasswordManager:
    def __init__(
        self,
        client: MailcowClient,
        audit: AuditLogger,
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._audit = audit
        self._ttl = ttl
        self._clock = clock
        self._creds: dict[str, _Credential] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False

    # -- public API -------------------------------------------------------------

    @asynccontextmanager
    async def lease(self, mailbox: str) -> AsyncIterator[Lease]:
        """Yield credentials for ``mailbox`` (already validated and allowed).

        Reuses a cached, unexpired app password or creates one. On exit the
        lease is released; an expired credential is deleted once unused.
        """
        if self._closed:
            raise TempPasswordError("server is shutting down")
        lock = self._lock_for(mailbox)
        async with lock:
            cred = self._creds.get(mailbox)
            if cred is not None and self._expired(cred):
                if cred.leases == 0:
                    await self._delete(cred, reason="expired")
                    cred = None
                # else: still in use by another call — reuse it until released.
            if cred is None:
                cred = await self._create(mailbox)
                self._creds[mailbox] = cred
            cred.leases += 1
        try:
            yield Lease(username=cred.mailbox, password=cred.password)
        finally:
            async with lock:
                cred.leases -= 1
                if cred.leases == 0 and (self._expired(cred) or self._closed):
                    await self._delete(cred, reason="expired")

    async def sweep(self) -> None:
        """Delete every unused credential whose TTL has expired."""
        for mailbox in list(self._creds):
            lock = self._lock_for(mailbox)
            async with lock:
                cred = self._creds.get(mailbox)
                if cred is not None and cred.leases == 0 and self._expired(cred):
                    await self._delete(cred, reason="expired")

    def start_sweeper(self, interval: float | None = None) -> None:
        """Run :meth:`sweep` periodically in the background (needs a running loop)."""
        if self._sweeper is not None:
            return
        period = interval if interval is not None else max(5.0, min(60.0, self._ttl / 4))

        async def _loop() -> None:
            while True:
                await asyncio.sleep(period)
                try:
                    await self.sweep()
                except Exception as e:  # keep sweeping
                    logger.warning("temp app password sweep failed: %s", sanitize_exception(e))

        self._sweeper = asyncio.create_task(_loop())

    async def aclose(self) -> None:
        """Stop the sweeper and delete every credential this server created.

        Credentials still leased by an in-flight call are deleted when that
        call ends. Best-effort: failures are logged; the startup cleanup of
        the next run removes anything left behind.
        """
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):
                pass
            self._sweeper = None
        for mailbox in list(self._creds):
            async with self._lock_for(mailbox):
                cred = self._creds.get(mailbox)
                if cred is not None and cred.leases == 0:
                    await self._delete(cred, reason="shutdown")

    async def cleanup_leftovers(self, allowlist: MailboxAllowList) -> int:
        """Delete ``mcp-temp-*`` app passwords in the allowed mailboxes.

        With ``*`` the mailboxes come from the admin API's mailbox list (the
        explicitly listed ones are always included). Every failure is logged
        as a warning and skipped; this never raises. Returns how many app
        passwords were deleted.
        """
        mailboxes: set[str] = set(allowlist.mailboxes)
        if allowlist.allow_all:
            try:
                data = await self._client.get("/api/v1/get/mailbox/all")
                for m in _items(data):
                    mb = normalize_mailbox(m.get("username"))
                    if mb:
                        mailboxes.add(mb)
            except Exception as e:
                logger.warning(
                    "startup cleanup: could not list mailboxes: %s", sanitize_exception(e)
                )
        deleted = 0
        for mb in sorted(mailboxes):
            try:
                ids = [
                    int(item["id"])
                    for item in _items(await self._list(mb))
                    if str(item.get("name", "")).startswith(TEMP_PREFIX) and "id" in item
                ]
                if not ids:
                    continue
                await self._client.post("/api/v1/delete/app-passwd", ids)
                deleted += len(ids)
                self._audit.write(
                    action="temp_app_password_cleanup",
                    params={"mailbox": mb, "count": len(ids)},
                    result="ok",
                    duration_ms=0,
                )
            except Exception as e:
                logger.warning(
                    "startup cleanup failed for one mailbox: %s", sanitize_exception(e)
                )
        if deleted:
            logger.info("startup cleanup: deleted %d leftover temp app password(s)", deleted)
        return deleted

    @property
    def cached_mailboxes(self) -> Iterable[str]:
        return tuple(self._creds)

    # -- internals ----------------------------------------------------------------

    def _lock_for(self, mailbox: str) -> asyncio.Lock:
        lock = self._locks.get(mailbox)
        if lock is None:
            lock = self._locks[mailbox] = asyncio.Lock()
        return lock

    def _expired(self, cred: _Credential) -> bool:
        return self._clock() >= cred.expires_at

    async def _list(self, mailbox: str) -> Any:
        return await self._client.get(f"/api/v1/get/app-passwd/all/{mailbox}")

    async def _create(self, mailbox: str) -> _Credential:
        app_name = generate_app_name()
        password = generate_password()
        register_secret(password)
        start = time.perf_counter()
        try:
            await self._client.post(
                "/api/v1/add/app-passwd",
                {
                    "username": mailbox,
                    "app_name": app_name,
                    "app_passwd": password,
                    "app_passwd2": password,
                    "protocols": list(TEMP_PROTOCOLS),
                    "active": 1,
                },
            )
            # add/app-passwd does not return the id: find it by the unique name.
            matches = [
                item for item in _items(await self._list(mailbox))
                if item.get("name") == app_name and "id" in item
            ]
            if len(matches) != 1:
                raise TempPasswordError(
                    "temporary app password was not found after creating it "
                    "(it will be removed by the next startup cleanup)"
                )
            app_id = int(matches[0]["id"])
        except Exception as e:
            unregister_secret(password)
            self._audit.write(
                action="temp_app_password_create",
                params={"mailbox": mailbox, "app_name": app_name},
                result="err",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error=sanitize_exception(e),
            )
            if isinstance(e, TempPasswordError):
                raise
            raise TempPasswordError(
                f"could not create a temporary app password: {sanitize_exception(e)}"
            ) from None
        self._audit.write(
            action="temp_app_password_create",
            params={"mailbox": mailbox, "app_name": app_name, "id": app_id},
            result="ok",
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
        return _Credential(
            mailbox=mailbox,
            app_name=app_name,
            app_id=app_id,
            password=password,
            expires_at=self._clock() + self._ttl,
        )

    async def _delete(self, cred: _Credential, *, reason: str) -> None:
        """Delete ``cred`` via the API and forget it (caller holds the lock).

        The credential is dropped from the cache even if the API call fails,
        so it is never handed out again; the startup cleanup catches it.
        """
        if self._creds.get(cred.mailbox) is cred:
            del self._creds[cred.mailbox]
        start = time.perf_counter()
        params = {
            "mailbox": cred.mailbox,
            "app_name": cred.app_name,
            "id": cred.app_id,
            "reason": reason,
        }
        try:
            await self._client.post("/api/v1/delete/app-passwd", [cred.app_id])
        except Exception as e:
            safe = sanitize_exception(e)
            logger.warning("could not delete a temporary app password: %s", safe)
            self._audit.write(
                action="temp_app_password_delete",
                params=params,
                result="err",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error=safe,
            )
        else:
            self._audit.write(
                action="temp_app_password_delete",
                params=params,
                result="ok",
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        finally:
            unregister_secret(cred.password)
