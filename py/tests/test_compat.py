"""mcp 1.x / 2.x compatibility (``mcp_mailcow._compat``).

These tests run against whichever ``mcp`` major is installed and assert the
behaviour documented for that path. CI or a local matrix should run the suite
once with mcp 1.x and once with mcp 2.x.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import respx
from mcp.server import Server
from starlette.testclient import TestClient

from mcp_mailcow import _compat
from mcp_mailcow.config import HttpConfig
from mcp_mailcow.http_app import create_app
from mcp_mailcow.schema import load_schema, tools_for_mode
from mcp_mailcow.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[2]
MAILCOW_URL = "https://mail.example.test"
TOKEN = "s3cret-test-token-abcdef"
V2 = not _compat.DECORATOR_API


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MAILCOW_ADMIN_URL", MAILCOW_URL)
    monkeypatch.setenv("MAILCOW_ADMIN_API_KEY", "dummy-api-key-0123456789")
    monkeypatch.setenv("MCP_MAILCOW_AUDIT_LOG", str(tmp_path / "audit.log"))


def _client(write: bool) -> TestClient:
    built = build_server("admin", enable_write_tools=write)
    cfg = HttpConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    return TestClient(create_app(built, cfg))


@pytest.fixture
def ro_client(admin_env: None) -> Iterator[TestClient]:
    with _client(write=False) as c:
        yield c


@pytest.fixture
def rw_client(admin_env: None) -> Iterator[TestClient]:
    with _client(write=True) as c:
        yield c


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    r = client.post("/mcp", content=json.dumps(body), headers={
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    })
    assert r.status_code == HTTPStatus.OK, r.text
    result: dict[str, Any] = r.json()["result"]
    return result


def _text(result: dict[str, Any]) -> str:
    return "".join(c["text"] for c in result["content"])


# --- feature detection -------------------------------------------------------


def test_detection_matches_installed_api() -> None:
    assert _compat.DECORATOR_API == hasattr(Server, "list_tools")


# --- isError per path ----------------------------------------------------------


def test_success_is_not_error(ro_client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MAILCOW_URL}/api/v1/get/status/version").respond(json={"version": "x"})
        res = _call(ro_client, "server_version", {})
    assert '"version": "x"' in _text(res)
    assert res.get("isError", False) is False


def test_gate_refusal_is_error_on_v2_only(ro_client: TestClient) -> None:
    res = _call(ro_client, "queue_flush", {})
    assert "disabled" in _text(res)
    assert res.get("isError", False) is V2


def test_tool_failure_is_error_on_v2_only(ro_client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MAILCOW_URL}/api/v1/get/status/version").respond(500, text="boom")
        res = _call(ro_client, "server_version", {})
    assert _text(res).startswith("error:")
    assert res.get("isError", False) is V2


def test_unknown_tool_is_error_on_v2_only(ro_client: TestClient) -> None:
    res = _call(ro_client, "no_such_tool", {})
    assert "unknown tool" in _text(res)
    assert res.get("isError", False) is V2


def test_confirmation_required_is_error_on_v2_only(rw_client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(host="mail.example.test").respond(json={})
        res = _call(rw_client, "domain_delete", {"domain": "x.test", "confirm": False})
        assert route.call_count == 0
    assert "confirm: true" in _text(res)
    assert res.get("isError", False) is V2


def test_input_validation_on_both_paths(rw_client: TestClient) -> None:
    # Missing required 'domain': 1.x's decorator and the 2.x adapter both
    # reject it before the handler runs, with the same message and isError.
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(host="mail.example.test").respond(json={})
        res = _call(rw_client, "domain_delete", {"confirm": True})
        assert route.call_count == 0
    assert _text(res).startswith("Input validation error:")
    assert res["isError"] is True


# --- stdio, end to end in a subprocess ----------------------------------------
# ``python -m mcp_mailcow`` (not the repo-root main.py) so no local .env is read.


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MAILCOW_", "MCP_MAILCOW_"))}
    env.update(extra)
    return env


def test_stdio_initialize_and_tools_list(tmp_path: Path) -> None:
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    env = _clean_env(
        PYTHONPATH=str(REPO_ROOT / "py" / "src"),
        MAILCOW_ADMIN_URL="http://127.0.0.1:9",  # never contacted
        MAILCOW_ADMIN_API_KEY="dummy-api-key-0123456789",
        MCP_MAILCOW_AUDIT_LOG=str(tmp_path / "audit.log"),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "mcp_mailcow", "--mode", "admin", "--transport", "stdio"],
        input="".join(json.dumps(m) + "\n" for m in msgs),
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=tmp_path, timeout=60, check=False,
    )
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines()) if "id" in r}
    assert replies[1]["result"]["serverInfo"]["name"] == "mcp-mailcow", proc.stderr
    tools = replies[2]["result"]["tools"]
    # stdio defaults to write tools enabled: every admin tool is listed
    assert {t["name"] for t in tools} == {
        t["name"] for t in tools_for_mode(load_schema(), "admin")
    }
    assert all("inputSchema" in t for t in tools)
