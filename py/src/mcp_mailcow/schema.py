"""Load and validate the tool schema YAML."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml

Mode = Literal["user", "admin"]


def load_schema() -> dict[str, Any]:
    """Load tools-schema.yaml shipped with the package.

    Lookup order:
    1. The YAML inside the installed package (`mcp_mailcow/tools-schema.yaml`).
       This is the standard path for both wheel and editable installs.
    2. The repo-root YAML (`../../../tools-schema.yaml` relative to this file).
       Fallback for unconventional dev setups.
    """
    # 1. Try inside the package
    pkg_path = files("mcp_mailcow").joinpath("tools-schema.yaml")
    try:
        with pkg_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        pass

    # 2. Fallback to repo root (for `pip install -e .` setups where data files
    # haven't been installed)
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent.parent / "tools-schema.yaml",  # py/src/mcp_mailcow/ → repo root
        here.parent.parent / "tools-schema.yaml",  # alternate layouts
    ]
    for candidate in candidates:
        if candidate.exists():
            with candidate.open("r", encoding="utf-8") as f:
                return yaml.safe_load(f)

    raise FileNotFoundError(
        "tools-schema.yaml not found. Tried package data and repo root. "
        "If running from a dev checkout, ensure the YAML is at the repo root "
        "or copied into py/src/mcp_mailcow/."
    )


def tools_for_mode(schema: dict[str, Any], mode: Mode) -> list[dict[str, Any]]:
    """Return the list of tool definitions for the given mode."""
    return [t for t in schema.get("tools", []) if t.get("mode") == mode]


def is_readonly(tool_def: dict[str, Any]) -> bool:
    """True only when the schema explicitly marks the tool ``readonly: true``.

    A missing or non-boolean value counts as a write tool (fail closed).
    """
    return tool_def.get("readonly") is True


def is_write_call(tool_def: dict[str, Any], arguments: dict[str, Any] | None) -> bool:
    """Whether a call must be refused when write tools are disabled.

    Write tools always are. A read-only tool is too when the call sets one of
    its ``write_params`` (e.g. ``read_message`` with ``mark_read: true``).
    """
    if not is_readonly(tool_def):
        return True
    args = arguments or {}
    return any(args.get(p) for p in tool_def.get("write_params") or ())


def visible_tools(
    tool_defs: list[dict[str, Any]], enable_write_tools: bool
) -> list[dict[str, Any]]:
    """Tool definitions to advertise in tools/list."""
    if enable_write_tools:
        return list(tool_defs)
    return [t for t in tool_defs if is_readonly(t)]


def to_mcp_tool(tool_def: dict[str, Any]) -> dict[str, Any]:
    """Convert our YAML tool def into the MCP Tool schema format.

    MCP expects:
        {"name": str, "description": str, "inputSchema": {JSON Schema}}
    """
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param_def in (tool_def.get("params") or {}).items():
        # Defensive: params may be empty dict {} for no-arg tools
        prop: dict[str, Any] = {"type": param_def.get("type", "string")}
        if "description" in param_def:
            prop["description"] = param_def["description"]
        if "default" in param_def:
            prop["default"] = param_def["default"]
        if "enum" in param_def:
            prop["enum"] = param_def["enum"]
        if "items" in param_def:
            prop["items"] = param_def["items"]
        if "format" in param_def:
            prop["format"] = param_def["format"]
        properties[param_name] = prop
        if param_def.get("required"):
            required.append(param_name)

    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        input_schema["required"] = required

    return {
        "name": tool_def["name"],
        "description": tool_def.get("description", "").strip(),
        "inputSchema": input_schema,
    }
