"""HTTP transport: bearer auth, /health, Host handling and the write-tool gate.

Everything runs in-process through Starlette's TestClient with dummy env
vars; Mailcow itself is mocked with respx (no network access).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml
from starlette.testclient import TestClient

from mcp_mailcow.config import HttpConfig
from mcp_mailcow.http_app import create_app
from mcp_mailcow.server import build_server

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "tools-schema.yaml"
MAILCOW_URL = "https://mail.example.test"
API_KEY = "dummy-api-key-0123456789"
TOKEN = "s3cret-test-token-abcdef"
ACCEPT = "application/json, text/event-stream"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch, audit_log: Path) -> None:
    monkeypatch.setenv("MAILCOW_ADMIN_URL", MAILCOW_URL)
    monkeypatch.setenv("MAILCOW_ADMIN_API_KEY", API_KEY)
    monkeypatch.setenv("MCP_MAILCOW_AUDIT_LOG", str(audit_log))


@pytest.fixture
def user_env(monkeypatch: pytest.MonkeyPatch, audit_log: Path) -> None:
    monkeypatch.setenv("MAILCOW_HOST", "mail.example.test")
    monkeypatch.setenv("MAILCOW_MAIL_USER", "user@example.test")
    monkeypatch.setenv("MAILCOW_MAIL_PASS", "dummy-mail-pass")
    monkeypatch.setenv("MCP_MAILCOW_AUDIT_LOG", str(audit_log))


def _client(
    mode: str = "admin",
    *,
    write: bool = False,
    token: str | None = TOKEN,
    allowed_hosts: tuple[str, ...] = (),
) -> TestClient:
    built = build_server(mode, enable_write_tools=write)  # type: ignore[arg-type]
    cfg = HttpConfig(host="127.0.0.1", port=0, auth_token=token, allowed_hosts=allowed_hosts)
    return TestClient(create_app(built, cfg))


@pytest.fixture
def client(admin_env: None) -> Iterator[TestClient]:
    with _client() as c:
        yield c


def _rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    token: str | None = TOKEN,
    headers: dict[str, str | bytes] | None = None,
) -> Any:
    h: dict[str, str | bytes] = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    h.update(headers or {})
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", content=json.dumps(body), headers=h)


def _tool_names(resp: Any) -> set[str]:
    assert resp.status_code == HTTPStatus.OK, resp.text
    return {t["name"] for t in resp.json()["result"]["tools"]}


def _call_text(resp: Any) -> str:
    assert resp.status_code == HTTPStatus.OK, resp.text
    return "".join(c["text"] for c in resp.json()["result"]["content"])


# --- /health and bearer auth -------------------------------------------------


def test_health_ok_without_token(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {"status": "ok"}


def test_missing_token_is_401(client: TestClient) -> None:
    r = _rpc(client, "tools/list", token=None)
    assert r.status_code == HTTPStatus.UNAUTHORIZED
    assert r.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in r.text


def test_non_bearer_scheme_is_401(client: TestClient) -> None:
    r = _rpc(client, "tools/list", token=None, headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("bad", ["wrong-token", TOKEN + "x", TOKEN[:-1], "", "tökén"])
def test_wrong_token_is_401(client: TestClient, bad: str) -> None:
    # raw UTF-8 bytes: non-ASCII header values must not crash the comparison
    r = _rpc(client, "tools/list", token=None,
             headers={"Authorization": f"Bearer {bad}".encode()})
    assert r.status_code == HTTPStatus.UNAUTHORIZED
    assert "invalid_token" in r.headers["www-authenticate"] or bad == ""
    assert TOKEN not in r.text


def test_correct_token_passes(client: TestClient) -> None:
    assert _tool_names(_rpc(client, "tools/list"))


def test_bearer_scheme_case_insensitive(client: TestClient) -> None:
    r = _rpc(client, "tools/list", token=None, headers={"Authorization": f"bearer {TOKEN}"})
    assert r.status_code == HTTPStatus.OK


def test_no_auth_mode_serves_without_token(admin_env: None) -> None:
    with _client(token=None) as c:
        assert _rpc(c, "tools/list", token=None).status_code == HTTPStatus.OK


# --- Host header (reverse proxy) ---------------------------------------------


def test_proxy_host_header_accepted_by_default(client: TestClient) -> None:
    r = _rpc(client, "tools/list", headers={"Host": "mcp.public.example.org"})
    assert r.status_code == HTTPStatus.OK


def test_allowed_hosts_enforced_when_configured(admin_env: None) -> None:
    with _client(allowed_hosts=("mcp.example.org",)) as c:
        bad = _rpc(c, "tools/list", headers={"Host": "evil.example"})
        assert bad.status_code == HTTPStatus.MISDIRECTED_REQUEST
        good = _rpc(c, "tools/list", headers={"Host": "mcp.example.org"})
        assert good.status_code == HTTPStatus.OK
        # /health stays reachable for probes regardless of Host
        assert c.get("/health", headers={"Host": "10.0.0.5:8000"}).status_code == HTTPStatus.OK


# --- write-tool gate ---------------------------------------------------------


def test_tools_list_hides_write_tools_when_disabled(
    client: TestClient, schema: dict[str, Any]
) -> None:
    admin = [t for t in schema["tools"] if t["mode"] == "admin"]
    names = _tool_names(_rpc(client, "tools/list"))
    assert names == {t["name"] for t in admin if t["readonly"] is True}
    for hidden in ("domain_delete", "mailbox_set_password", "app_password_create",
                   "domain_admin_sso_token", "send_test_mail", "transport_list"):
        assert hidden not in names


def test_tools_list_shows_all_when_enabled(admin_env: None, schema: dict[str, Any]) -> None:
    with _client(write=True) as c:
        names = _tool_names(_rpc(c, "tools/list"))
    assert names == {t["name"] for t in schema["tools"] if t["mode"] == "admin"}


def test_write_call_refused_when_disabled(client: TestClient, audit_log: Path) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(host="mail.example.test").respond(json={"ok": True})
        r = _rpc(client, "tools/call", {"name": "queue_flush", "arguments": {}})
        assert route.call_count == 0  # never reached Mailcow
    text = _call_text(r)
    assert "disabled" in text and "MCP_MAILCOW_ENABLE_WRITE_TOOLS" in text
    entries = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert entries[-1]["action"] == "queue_flush"
    assert entries[-1]["result"] == "denied"


def test_denied_call_masks_secret_args_in_audit(client: TestClient, audit_log: Path) -> None:
    args = {"email": "a@example.test", "password": "Very-Secret-Pw1", "confirm": True}
    r = _rpc(client, "tools/call", {"name": "mailbox_set_password", "arguments": args})
    assert "disabled" in _call_text(r)
    assert "Very-Secret-Pw1" not in audit_log.read_text(encoding="utf-8")


def test_read_call_allowed_when_disabled(client: TestClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{MAILCOW_URL}/api/v1/get/status/version").respond(
            json={"version": "2026-01"}
        )
        r = _rpc(client, "tools/call", {"name": "server_version", "arguments": {}})
        assert route.call_count == 1
        assert route.calls.last.request.headers["X-API-Key"] == API_KEY
    assert "2026-01" in _call_text(r)


def test_write_call_runs_when_enabled(admin_env: None) -> None:
    with _client(write=True) as c, respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{MAILCOW_URL}/api/v1/edit/mailq").respond(
            json=[{"type": "success"}]
        )
        r = _rpc(c, "tools/call", {"name": "queue_flush", "arguments": {}})
        assert route.call_count == 1
    assert "success" in _call_text(r)


def test_write_params_on_readonly_tool_refused(user_env: None) -> None:
    with _client("user") as c:
        assert "read_message" in _tool_names(_rpc(c, "tools/list"))
        assert "send_message" not in _tool_names(_rpc(c, "tools/list"))
        r = _rpc(c, "tools/call",
                 {"name": "read_message", "arguments": {"uid": 1, "mark_read": True}})
    assert "disabled" in _call_text(r)


def test_secrets_never_logged(
    client: TestClient, audit_log: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{MAILCOW_URL}/api/v1/get/status/version").respond(
            500, text=f"boom api_key={API_KEY}"
        )
        _rpc(client, "tools/call", {"name": "server_version", "arguments": {}})
    _rpc(client, "tools/list", token="wrong")
    _rpc(client, "tools/call", {"name": "queue_flush", "arguments": {}})
    combined = caplog.text + audit_log.read_text(encoding="utf-8")
    assert TOKEN not in combined
    assert API_KEY not in combined
