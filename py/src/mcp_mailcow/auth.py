"""Bearer token authentication for the HTTP transport.

Same rules as nginx-proxy-manager-mcp's ``auth.py`` (case-insensitive
``Bearer`` scheme, constant-time comparison with ``hmac.compare_digest``,
``/health`` exempt), written as a plain ASGI middleware instead of
``BaseHTTPMiddleware`` so streamed (SSE) responses from ``/mcp`` pass through
untouched.

Neither the configured token nor the token a client supplied is ever logged
or echoed back in a response.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("mcp_mailcow.auth")

_SCHEME = "bearer "


class BearerAuthMiddleware:
    """Reject HTTP requests without a valid ``Authorization: Bearer <token>``."""

    def __init__(
        self, app: ASGIApp, token: str, exempt_paths: Iterable[str] = ("/health",)
    ) -> None:
        if not token:
            raise ValueError("BearerAuthMiddleware requires a non-empty token")
        self.app = app
        # surrogateescape: never raise on odd input, compare raw bytes.
        self._token = token.encode("utf-8", "surrogateescape")
        self._exempt = frozenset(exempt_paths)

    def __repr__(self) -> str:  # never expose the token
        return f"{type(self).__name__}(exempt_paths={sorted(self._exempt)!r})"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._exempt:
            await self.app(scope, receive, send)
            return

        auth = Headers(scope=scope).get("authorization", "")
        # RFC 7235 §2.1: the auth scheme is case-insensitive.
        if auth[: len(_SCHEME)].lower() != _SCHEME:
            response = JSONResponse(
                {"error": "Missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        supplied = auth[len(_SCHEME) :].strip().encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(supplied, self._token):
            client = scope.get("client")
            logger.warning(
                "rejected request with invalid bearer token from %s",
                client[0] if client else "unknown",
            )
            # RFC 6750 §3.1: invalid token -> 401 with error="invalid_token".
            response = JSONResponse(
                {"error": "Invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
