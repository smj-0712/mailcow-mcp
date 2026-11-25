"""Load and validate the tool schema YAML."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml

Mode = Literal["user", "admin", "all"]

# --- all mode policy ----------------------------------------------------------
#
# In ``all`` mode a single server holds the admin API key *and* reads mail from
# many mailboxes. Mail bodies are untrusted external input, so a message can
# carry a prompt injection that asks the model to call tools. The constants
# below limit what such an injection can reach. They only apply to ``all``
# mode; the ``admin`` and ``user`` modes keep their previous behaviour.

#: Tools that send mail to arbitrary recipients. In ``all`` mode their
#: visibility is controlled by MCP_MAILCOW_ENABLE_MAIL_SEND alone (default
#: off), separately from MCP_MAILCOW_ENABLE_WRITE_TOOLS: sending is the most
#: direct way to exfiltrate mail read from another mailbox, so it gets its own
#: explicit opt-in. save_draft is not here: it only APPENDs to Drafts.
SEND_TOOLS: frozenset[str] = frozenset({
    "send_message",
    "reply_to_message",
    "forward_message",
})

#: Admin tools hidden (and refused) in ``all`` mode even when write tools are
#: enabled, unless MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS=true. Each one can take
#: over an account or hand out credentials/admin sessions (passwords, app
#: passwords, domain-admin accounts and SSO tokens, ACLs, OAuth2 clients,
#: CORS / identity-provider settings). A prompt injection inside a mail body
#: must not be able to reach them. app_password_list is read-only but lists
#: the credentials this server itself creates, so it is hidden too.
SENSITIVE_TOOLS: frozenset[str] = frozenset({
    "mailbox_set_password",
    "app_password_create",
    "app_password_delete",
    "app_password_list",
    "domain_admin_sso_token",
    "domain_admin_create",
    "domain_admin_update",
    "domain_admin_set_acl",
    "domain_admin_delete",
    "mailbox_set_acl",
    "oauth2_client_create",
    "oauth2_client_delete",
    "cors_settings_update",
    "identity_provider_settings_update",
})

#: Name of the argument injected into every user tool in ``all`` mode.
MAILBOX_ARG = "mailbox"


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
    """Return the list of tool definitions for the given mode.

    ``all`` returns the admin tools followed by the user tools (tool names are
    unique across both modes, which test_schema checks).
    """
    tools = schema.get("tools", [])
    if mode == "all":
        return [t for t in tools if t.get("mode") == "admin"] + [
            t for t in tools if t.get("mode") == "user"
        ]
    return [t for t in tools if t.get("mode") == mode]


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


@dataclass(frozen=True)
class ToolPolicy:
    """Which tools are listed and which calls are refused.

    ``send_gate`` / ``hide_sensitive`` are only set in ``all`` mode; with the
    defaults the policy is exactly the previous write gate (``visible_tools``
    and ``is_write_call``).
    """

    enable_write_tools: bool
    #: None: send tools follow ``enable_write_tools`` (admin/user modes).
    #: True/False: send tools follow this flag only (all mode).
    enable_mail_send: bool | None = None
    #: Hide and refuse SENSITIVE_TOOLS (all mode unless explicitly allowed).
    hide_sensitive: bool = False

    def refusal(self, tool_def: dict[str, Any], arguments: dict[str, Any] | None) -> str | None:
        """Reason code a call must be refused for, or None if it may run:
        ``"sensitive"``, ``"send"`` or ``"write"``."""
        name = tool_def.get("name")
        if self.hide_sensitive and name in SENSITIVE_TOOLS:
            return "sensitive"
        if self.enable_mail_send is not None and name in SEND_TOOLS:
            return None if self.enable_mail_send else "send"
        if not self.enable_write_tools and is_write_call(tool_def, arguments):
            return "write"
        return None

    def visible(self, tool_def: dict[str, Any]) -> bool:
        """Whether the tool is advertised in tools/list."""
        name = tool_def.get("name")
        if self.hide_sensitive and name in SENSITIVE_TOOLS:
            return False
        if self.enable_mail_send is not None and name in SEND_TOOLS:
            return self.enable_mail_send
        return self.enable_write_tools or is_readonly(tool_def)

    def visible_tools(self, tool_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [t for t in tool_defs if self.visible(t)]


def to_mcp_tool(tool_def: dict[str, Any], *, inject_mailbox: bool = False) -> dict[str, Any]:
    """Convert our YAML tool def into the MCP Tool schema format.

    MCP expects:
        {"name": str, "description": str, "inputSchema": {JSON Schema}}

    ``inject_mailbox`` (all mode, user tools) adds a required ``mailbox``
    string argument naming the mailbox to act on. tools-schema.yaml itself is
    not changed.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    if inject_mailbox:
        if MAILBOX_ARG in (tool_def.get("params") or {}):
            raise ValueError(f"tool {tool_def['name']} already has a '{MAILBOX_ARG}' param")
        properties[MAILBOX_ARG] = {
            "type": "string",
            "description": "Mailbox address to act on (must be allowed by "
            "MCP_MAILCOW_MAILBOXES).",
        }
        required.append(MAILBOX_ARG)

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
