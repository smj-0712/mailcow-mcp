"""Compatibility layer between the ``mcp`` 1.x and 2.x low-level Server APIs.

Everything that depends on which ``mcp`` major version is installed lives
here; the rest of the package only calls :func:`build_lowlevel_server`.

The API shape is chosen by **feature detection**, not by parsing a version
string:

* **1.x** (``>=1.10``): ``Server`` has ``list_tools()`` / ``call_tool()``
  decorator methods. Handlers return ``list[Tool]`` / ``list[TextContent]``.
* **2.x**: the decorators are gone; handlers are passed to the constructor as
  ``on_list_tools`` / ``on_call_tool`` with the signature
  ``async (ctx, params) -> ListToolsResult / CallToolResult``.

Behavioural differences between the two paths (intentional):

* ``isError`` — on 1.x every result of our handler is sent with
  ``isError: false``, including write-gate refusals and tool failures (the
  decorator only sets it when the handler *raises*, and ours never does).
  That is the pre-existing behaviour and is kept unchanged. On 2.x the
  result is built here, so refusals, unknown tools, confirmation prompts and
  tool failures are sent with ``isError: true``. The text is identical.
* **Input validation** — 1.x's ``call_tool()`` decorator validates the
  arguments of listed tools against their ``inputSchema`` (jsonschema) and
  answers ``Input validation error: ...`` with ``isError: true``. 2.x's
  low-level server does not, so the 2.x path performs the same check here
  (same message, same "listed tools only" scope) before dispatching.

Both paths call the same ``dispatch`` coroutine, which holds the actual
handler logic (write gate, audit log, error sanitising, ConfirmationRequired).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import jsonschema  # type: ignore[import-untyped,unused-ignore]
import mcp.types as mcp_types
from mcp.server import Server
from mcp.types import TextContent, Tool

#: True on mcp 1.x (decorator API), False on mcp 2.x (constructor handlers).
DECORATOR_API: bool = hasattr(Server, "list_tools") and hasattr(Server, "call_tool")


@dataclass(frozen=True)
class ToolOutcome:
    """Transport-independent result of one tools/call."""

    text: str
    is_error: bool = False


Dispatch = Callable[[str, dict[str, Any]], Awaitable[ToolOutcome]]


def _make_server(name: str, version: str, **handlers: Any) -> Any:
    try:
        return Server(name, version=version, **handlers)
    except TypeError:
        if handlers:  # a 2.x-style constructor must accept the handlers
            raise
        return Server(name)  # very old 1.x without the version argument


def _build_v1(name: str, version: str, listed: list[Tool], dispatch: Dispatch) -> Any:
    server = _make_server(name, version)

    @server.list_tools()  # type: ignore[untyped-decorator,no-untyped-call,unused-ignore]
    async def list_tools() -> list[Tool]:
        return listed

    @server.call_tool()  # type: ignore[untyped-decorator,unused-ignore]
    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> list[TextContent]:
        # is_error is deliberately dropped: unchanged 1.x behaviour (see module doc).
        outcome = await dispatch(tool_name, arguments or {})
        return [TextContent(type="text", text=outcome.text)]

    return server


def _build_v2(name: str, version: str, listed: list[Tool], dispatch: Dispatch) -> Any:
    # Typed as Any: mypy may be checking against the 1.x stubs (camelCase fields).
    call_tool_result: Any = mcp_types.CallToolResult
    list_tools_result: Any = mcp_types.ListToolsResult

    schemas: Mapping[str, Any] = {
        t.name: t.model_dump(by_alias=True, exclude_none=True)["inputSchema"] for t in listed
    }

    async def on_list_tools(_ctx: Any, _params: Any) -> Any:
        return list_tools_result(tools=listed)

    async def on_call_tool(_ctx: Any, params: Any) -> Any:
        tool_name: str = params.name
        arguments: dict[str, Any] = dict(params.arguments or {})
        schema = schemas.get(tool_name)
        if schema is not None:
            try:
                jsonschema.validate(instance=arguments, schema=schema)
            except jsonschema.ValidationError as e:
                outcome = ToolOutcome(f"Input validation error: {e.message}", is_error=True)
            else:
                outcome = await dispatch(tool_name, arguments)
        else:
            outcome = await dispatch(tool_name, arguments)
        return call_tool_result(
            content=[TextContent(type="text", text=outcome.text)],
            is_error=outcome.is_error,
        )

    return _make_server(
        name, version, on_list_tools=on_list_tools, on_call_tool=on_call_tool
    )


def build_lowlevel_server(
    name: str, version: str, listed: list[Tool], dispatch: Dispatch
) -> Any:
    """Return an ``mcp.server.Server`` serving ``listed`` via ``dispatch``.

    The returned object is used only through the API both majors share:
    ``run(read, write, init_options)``, ``create_initialization_options()``
    and as ``app`` of ``StreamableHTTPSessionManager``.
    """
    if DECORATOR_API:
        return _build_v1(name, version, listed, dispatch)
    if "on_call_tool" in _init_params():
        return _build_v2(name, version, listed, dispatch)
    raise RuntimeError(
        "unsupported 'mcp' package: mcp.server.Server has neither the 1.x decorator "
        "API nor the 2.x on_list_tools/on_call_tool constructor. Install mcp>=1.10,<3."
    )


def _init_params() -> Mapping[str, Any]:
    import inspect  # noqa: PLC0415

    try:
        return inspect.signature(Server.__init__).parameters
    except (TypeError, ValueError):
        return {}
