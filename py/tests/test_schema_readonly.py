"""The `readonly` classification in tools-schema.yaml and packaging sync checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_mailcow.schema import is_readonly, is_write_call, load_schema, visible_tools

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_SCHEMA = REPO_ROOT / "tools-schema.yaml"
TOOL_COUNT = 125
PKG_SCHEMA = REPO_ROOT / "py" / "src" / "mcp_mailcow" / "tools-schema.yaml"

# Ambiguous / sensitive tools that must never be exposed on a read-only server.
MUST_BE_WRITE = {
    "domain_admin_sso_token", "app_password_create", "mailbox_set_password",
    "send_message", "reply_to_message", "forward_message", "send_test_mail",
    "oauth2_client_create", "transport_list", "relayhost_list", "sync_job_list",
    "oauth2_client_list", "logs_get_api",
}


@pytest.fixture(scope="module")
def tools() -> list[dict[str, Any]]:
    with ROOT_SCHEMA.open(encoding="utf-8") as f:
        return list(yaml.safe_load(f)["tools"])


def test_both_schema_copies_identical() -> None:
    assert ROOT_SCHEMA.read_bytes() == PKG_SCHEMA.read_bytes()


def test_every_tool_has_boolean_readonly(tools: list[dict[str, Any]]) -> None:
    assert len(tools) == TOOL_COUNT
    for t in tools:
        assert isinstance(t.get("readonly"), bool), f"{t['name']} needs readonly: true|false"


def test_sensitive_tools_are_not_readonly(tools: list[dict[str, Any]]) -> None:
    by_name = {t["name"]: t for t in tools}
    for name in MUST_BE_WRITE:
        assert by_name[name]["readonly"] is False, name


def test_mutating_verbs_are_not_readonly(tools: list[dict[str, Any]]) -> None:
    """Sanity net (not the classification itself): nothing named like a
    mutation may be marked readonly."""
    verbs = re.compile(r"_(create|update|delete|set|send|flush|release|learn|unban|duplicate)")
    for t in tools:
        if verbs.search(t["name"]):
            assert t["readonly"] is False, t["name"]


def test_write_params_only_on_readonly_tools(tools: list[dict[str, Any]]) -> None:
    for t in tools:
        wp = t.get("write_params")
        if wp is None:
            continue
        assert t["readonly"] is True, t["name"]
        for p in wp:
            assert p in (t.get("params") or {}), f"{t['name']}: unknown write_param {p}"


def test_gate_helpers() -> None:
    assert is_readonly({"readonly": True})
    assert not is_readonly({})  # missing -> write (fail closed)
    assert not is_readonly({"readonly": "true"})  # non-bool -> write
    rm = {"readonly": True, "write_params": ["mark_read"]}
    assert not is_write_call(rm, {"uid": 1})
    assert not is_write_call(rm, {"uid": 1, "mark_read": False})
    assert is_write_call(rm, {"uid": 1, "mark_read": True})
    assert is_write_call({"readonly": False}, None)
    defs = [{"name": "r", "readonly": True}, {"name": "w", "readonly": False}, {"name": "x"}]
    assert [t["name"] for t in visible_tools(defs, False)] == ["r"]
    assert len(visible_tools(defs, True)) == len(defs)


def test_packaged_schema_loads_with_readonly() -> None:
    schema = load_schema()
    assert all(isinstance(t.get("readonly"), bool) for t in schema["tools"])


def _norm(req: str) -> str:
    return req.replace(" ", "").lower()


def test_requirements_txt_matches_pyproject() -> None:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    with (REPO_ROOT / "py" / "pyproject.toml").open("rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    reqs = [
        line.strip()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert sorted(map(_norm, reqs)) == sorted(map(_norm, deps))
