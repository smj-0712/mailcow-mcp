"""MCP server construction and stdio transport.

The module is split in two layers:

* **Server construction** — :func:`build_server` loads the config for the
  chosen mode, builds the tool registry and audit logger, and wires them
  into an ``mcp.server.Server`` (``list_tools`` / ``call_tool`` handlers,
  including the write-tool gate). Nothing here knows about a transport.
* **Transport execution** — :func:`run_stdio` (below) and
  :func:`mcp_mailcow.http_app.run_http` both take the same
  :class:`BuiltServer`, so every transport shares the same handlers,
  registry and audit log.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
from .audit import AuditLogger
from .config import load_admin_config, load_user_config
from .exceptions import ConfirmationRequired
from .registry import ToolHandler, build_admin_registry, build_user_registry
from .schema import Mode, is_write_call, load_schema, to_mcp_tool, tools_for_mode, visible_tools
from .secret_sanitize import sanitize_exception

logger = logging.getLogger("mcp_mailcow")

WRITE_DISABLED_MSG = (
    "error: tool '{name}' is disabled on this server: write tools are turned off "
    "(MCP_MAILCOW_ENABLE_WRITE_TOOLS=false)."
)


@dataclass
class BuiltServer:
    """A configured MCP server plus the resources it owns."""

    server: Server[Any, Any]
    ctx: Any
    mode: Mode
    enable_write_tools: bool

    async def aclose(self) -> None:
        """Close persistent resources owned by the context (e.g. the
        MailcowClient httpx pool in admin mode). Best-effort."""
        aclose = getattr(self.ctx, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                logger.exception("error during context cleanup")


def create_server(
    tool_defs: list[dict[str, Any]],
    registry: dict[str, ToolHandler],
    audit: AuditLogger,
    *,
    enable_write_tools: bool,
) -> Server[Any, Any]:
    """Wire tool definitions and handlers into an ``mcp`` Server.

    When ``enable_write_tools`` is false, tools that are not ``readonly: true``
    in tools-schema.yaml are left out of tools/list, and calls to them (or to a
    read-only tool with one of its ``write_params`` set) are refused before the
    handler runs. Refusals are recorded in the audit log.
    """
    defs_by_name = {t["name"]: t for t in tool_defs}
    listed = [Tool(**to_mcp_tool(t)) for t in visible_tools(tool_defs, enable_write_tools)]

    try:
        server: Server[Any, Any] = Server("mcp-mailcow", version=__version__)
    except TypeError:  # very old mcp 1.x without the version argument
        server = Server("mcp-mailcow")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return listed

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        tool_def = defs_by_name.get(name)
        if tool_def is None or name not in registry:
            return [TextContent(type="text", text=f"error: unknown tool '{name}'")]
        if not enable_write_tools and is_write_call(tool_def, arguments):
            audit.write(
                action=name,
                params=arguments or {},
                result="denied",
                duration_ms=0,
                error="write tools disabled",
            )
            return [TextContent(type="text", text=WRITE_DISABLED_MSG.format(name=name))]
        handler = registry[name]
        try:
            result = await handler(arguments)
        except ConfirmationRequired as e:
            return [
                TextContent(
                    type="text",
                    text=f"⚠ {e}\nAdd `confirm: true` to the arguments and retry.",
                )
            ]
        except Exception as e:  # surface as text error, don't crash the server
            # Only the sanitized message is logged and returned: in HTTP mode
            # stderr usually ends up in a shared console (e.g. a panel), so
            # raw exception text — which may embed a secret — must not reach
            # it. The traceback is available at DEBUG level for local debugging.
            safe = sanitize_exception(e)
            logger.error("tool %s failed: %s", name, safe)
            logger.debug("tool %s traceback", name, exc_info=True)
            return [TextContent(type="text", text=f"error: {safe}")]
        text = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False, indent=2)
        )
        return [TextContent(type="text", text=text)]

    return server


def build_server(mode: Mode, *, enable_write_tools: bool) -> BuiltServer:
    """Load config from the environment and build the server for ``mode``.

    Exits with code 2 (via ``config._require``) if required env vars are missing.
    """
    tool_defs = tools_for_mode(load_schema(), mode)

    registry: dict[str, ToolHandler]
    if mode == "user":
        user_cfg = load_user_config()
        audit = AuditLogger(user_cfg.audit_log)
        registry, ctx = build_user_registry(user_cfg, audit)
    else:
        admin_cfg = load_admin_config()
        audit = AuditLogger(admin_cfg.audit_log)
        registry, ctx = build_admin_registry(admin_cfg, audit)

    server = create_server(tool_defs, registry, audit, enable_write_tools=enable_write_tools)
    return BuiltServer(server=server, ctx=ctx, mode=mode, enable_write_tools=enable_write_tools)


async def run_stdio(built: BuiltServer) -> None:
    """Serve ``built`` over stdin/stdout until the client disconnects."""
    server = built.server
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await built.aclose()


async def run_server(mode: Mode, enable_write_tools: bool = True) -> None:
    """Backward-compatible stdio entry point (pre-HTTP API)."""
    await run_stdio(build_server(mode, enable_write_tools=enable_write_tools))
