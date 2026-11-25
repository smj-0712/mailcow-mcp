"""Streamable HTTP transport: Starlette app + uvicorn around the same Server.

Endpoints:

* ``/mcp``    — MCP Streamable HTTP endpoint (``StreamableHTTPSessionManager``).
* ``/health`` — liveness probe, no authentication, no Mailcow call.

Design notes
------------
**Stateless mode.** The session manager runs with ``stateless=True``: every
POST to /mcp is handled by a fresh transport, and no ``Mcp-Session-Id`` is
kept in memory. Reasons:

* every tool here is a plain request/response call — the server never sends
  server-initiated requests (sampling, elicitation) or notifications that
  would need a long-lived session;
* panel-hosted / reverse-proxied deployments restart the process often, and
  stateful sessions live only in this process's memory, so after any restart
  every connected client would get "session not found" until it
  re-initialized;
* no per-session state to leak or to cap, and nothing breaks if a proxy ever
  spreads requests over several instances.

**JSON responses.** ``json_response=True`` returns each result as one
``application/json`` body instead of an SSE stream. Since no tool streams
progress, SSE adds nothing, and plain JSON is not affected by reverse proxies
that buffer or time out ``text/event-stream`` responses.

**Host header validation.** The SDK only validates Host/Origin (DNS-rebinding
protection) when ``security_settings`` enables it; left at ``None`` it is off,
so requests forwarded by a reverse proxy with an arbitrary public Host header
are accepted. Access control is the bearer token. Setting
``MCP_MAILCOW_ALLOWED_HOSTS`` (comma-separated, ``host:*`` wildcard ports
allowed) turns validation on with that allow-list; ``MCP_MAILCOW_ALLOWED_ORIGINS``
then lists the browser Origins accepted (requests without Origin always pass).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .auth import BearerAuthMiddleware
from .config import HttpConfig
from .server import BuiltServer

logger = logging.getLogger("mcp_mailcow.http")

MCP_PATH = "/mcp"
HEALTH_PATH = "/health"


class _MCPEndpoint:
    """ASGI endpoint forwarding /mcp to the session manager.

    An instance (not a function) so Starlette's ``Route`` passes the raw ASGI
    scope through instead of wrapping it in a request/response handler, and
    so ``/mcp`` is served without a ``Mount``'s ``/mcp/`` redirect.
    """

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _security_settings(cfg: HttpConfig) -> TransportSecuritySettings | None:
    if not cfg.allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(cfg.allowed_hosts),
        allowed_origins=list(cfg.allowed_origins),
    )


def create_app(built: BuiltServer, cfg: HttpConfig) -> Starlette:
    """Build the ASGI app. The app's lifespan runs the session manager and
    closes ``built``'s resources on shutdown."""
    manager = StreamableHTTPSessionManager(
        app=built.server,
        stateless=True,
        json_response=True,
        security_settings=_security_settings(cfg),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            await built.astart()
            async with manager.run():
                yield
        finally:
            await built.aclose()

    middleware = []
    if cfg.auth_token:
        middleware.append(
            Middleware(BearerAuthMiddleware, token=cfg.auth_token, exempt_paths=(HEALTH_PATH,))
        )

    return Starlette(
        routes=[
            Route(HEALTH_PATH, _health, methods=["GET"]),
            Route(MCP_PATH, _MCPEndpoint(manager)),
        ],
        middleware=middleware,
        lifespan=lifespan,
    )


def run_http(built: BuiltServer, cfg: HttpConfig) -> None:
    """Serve ``built`` over Streamable HTTP with uvicorn (blocking)."""
    logger.info(
        "starting MCP HTTP server: mode=%s, http://%s:%d%s, health %s",
        built.mode, cfg.host, cfg.port, MCP_PATH, HEALTH_PATH,
    )
    if cfg.auth_token:
        logger.info("bearer token authentication enabled")
    else:
        logger.warning(
            "AUTHENTICATION DISABLED (MCP_MAILCOW_ALLOW_NO_AUTH=true): anyone who can "
            "reach %s:%d can call the %s tools", cfg.host, cfg.port, built.mode,
        )
    logger.info(
        "write tools %s",
        "ENABLED" if built.enable_write_tools
        else "disabled (read-only; set MCP_MAILCOW_ENABLE_WRITE_TOOLS=true to enable)",
    )
    if built.policy is not None and built.policy.enable_mail_send is not None:
        logger.info(
            "mail send tools %s",
            "ENABLED" if built.policy.enable_mail_send
            else "disabled (set MCP_MAILCOW_ENABLE_MAIL_SEND=true to enable)",
        )
        logger.info(
            "sensitive admin tools %s",
            "hidden (MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS=false)" if built.policy.hide_sensitive
            else "ALLOWED (MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS=true)",
        )
    if cfg.allowed_hosts:
        logger.info("Host header allow-list: %s", ", ".join(cfg.allowed_hosts))

    uvicorn.run(create_app(built, cfg), host=cfg.host, port=cfg.port, log_level="info")
