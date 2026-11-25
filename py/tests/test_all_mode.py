"""All mode: admin + user tools in one server, per-mailbox temp app passwords.

The Mailcow REST API is a stateful fake served through respx (no network).
IMAP and SMTP are mocked at the user_tools level (``imap_session``,
``send_via_submission``, ``append_to_sent``): the real protocols are NOT
exercised here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml
from starlette.testclient import TestClient

from mcp_mailcow import __main__ as cli
from mcp_mailcow import user_tools
from mcp_mailcow.all_mode import build_all_registry
from mcp_mailcow.audit import AuditLogger
from mcp_mailcow.config import (
    HttpConfig,
    MailboxAllowList,
    env_mode,
    load_all_config,
    parse_mailbox_allowlist,
    write_tools_enabled,
)
from mcp_mailcow.exceptions import AccessDenied
from mcp_mailcow.http_app import create_app
from mcp_mailcow.mailcow_api import MailcowClient
from mcp_mailcow.schema import (
    SEND_TOOLS,
    SENSITIVE_TOOLS,
    ToolPolicy,
    to_mcp_tool,
    tools_for_mode,
)
from mcp_mailcow.secret_sanitize import register_secret, sanitize_text, unregister_secret
from mcp_mailcow.server import build_server
from mcp_mailcow.temp_app_passwords import (
    TEMP_PREFIX,
    TempAppPasswordManager,
    TempPasswordError,
)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "tools-schema.yaml"
URL = "https://mail.example.test"
API_KEY = "dummy-api-key-0123456789"
TOKEN = "s3cret-test-token-abcdef"
ACCEPT = "application/json, text/event-stream"
MB_A = "a@example.test"
MB_B = "b@example.test"
MB_OUTSIDE = "c@example.test"
EXIT_USAGE = 2
ALL_TOOLS = 125  # 104 admin + 21 user
DEFAULT_TOOLS = 41  # write/send off, sensitive hidden: 34 admin read + 7 user read
WRITE_TOOLS_ON = 108  # + write tools, minus 3 send and 14 sensitive
SEND_ON = 44  # default + 3 send tools
WRITE_AND_SEND_ON = 111
SENSITIVE_ALLOWED = 42  # default + app_password_list (the only read-only sensitive tool)
WRITE_AND_SENSITIVE_ON = 122
DEFAULT_TTL = 600
MIN_PASSWORD_LEN = 40


# =============================================================================
# Fakes
# =============================================================================


class FakeMailcow:
    """Stateful stand-in for the app-passwd / mailbox / domain endpoints."""

    def __init__(self, mailboxes: tuple[str, ...] = (MB_A, MB_B, MB_OUTSIDE)) -> None:
        self.apps: dict[str, list[dict[str, Any]]] = {mb: [] for mb in mailboxes}
        self.next_id = 100
        self.ops: list[tuple[str, Any]] = []
        self.add_payloads: list[dict[str, Any]] = []
        self.plain: dict[int, str] = {}  # id -> plaintext (test-side only)
        self.fail_add = False
        self.fail_mailbox_list = False

    def seed(self, mailbox: str, name: str) -> int:
        self.next_id += 1
        self.apps[mailbox].append(
            {"id": self.next_id, "name": name, "mailbox": mailbox, "password": "$hash$"}
        )
        return self.next_id

    def names(self, mailbox: str) -> list[str]:
        return [a["name"] for a in self.apps[mailbox]]

    def install(self, mock: respx.MockRouter) -> None:
        mock.post(f"{URL}/api/v1/add/app-passwd").mock(side_effect=self._add)
        mock.post(f"{URL}/api/v1/delete/app-passwd").mock(side_effect=self._delete)
        mock.get(url__regex=rf"{re.escape(URL)}/api/v1/get/app-passwd/all/(?P<mb>.+)$").mock(
            side_effect=self._list
        )
        mock.get(f"{URL}/api/v1/get/mailbox/all").mock(side_effect=self._mailboxes)
        mock.get(f"{URL}/api/v1/get/domain/all").mock(side_effect=self._domains)

    async def _add(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)  # yield, so concurrent callers can race
        body = json.loads(request.content)
        self.add_payloads.append(body)
        self.ops.append(("add", body["username"]))
        if self.fail_add:
            return httpx.Response(
                200, json=[{"type": "danger", "msg": ["password_complexity"]}]
            )
        self.next_id += 1
        self.apps[body["username"]].append(
            {"id": self.next_id, "name": body["app_name"], "mailbox": body["username"],
             "password": "$hash$"}
        )
        self.plain[self.next_id] = body["app_passwd"]
        return httpx.Response(200, json=[{"type": "success", "msg": ["app_passwd_added"]}])

    def _list(self, request: httpx.Request, mb: str) -> httpx.Response:
        self.ops.append(("list", mb))
        items = self.apps.get(mb, [])
        return httpx.Response(200, json=list(items) if items else {})

    def _delete(self, request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)
        assert all(isinstance(i, int) for i in ids)
        self.ops.append(("delete", ids))
        for mb in self.apps:
            self.apps[mb] = [a for a in self.apps[mb] if a["id"] not in ids]
        return httpx.Response(200, json=[{"type": "success", "msg": ["app_passwd_removed"]}])

    def _mailboxes(self, request: httpx.Request) -> httpx.Response:
        self.ops.append(("mailbox_list", None))
        if self.fail_mailbox_list:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=[{"username": mb} for mb in self.apps])

    def _domains(self, request: httpx.Request) -> httpx.Response:
        self.ops.append(("domain_list", None))
        return httpx.Response(200, json=[{"domain_name": "example.test"}])


class FakeIMAP:
    def __init__(self) -> None:
        self.selected: list[tuple[str, bool]] = []

    def list_folders(self) -> list[tuple[tuple[bytes, ...], bytes, str]]:
        return [((b"\\HasNoChildren",), b"/", "INBOX")]

    def select_folder(self, folder: str, readonly: bool = False) -> None:
        self.selected.append((folder, readonly))


@pytest.fixture
def mail_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mock IMAP/SMTP in user_tools; records logins and the thread used."""
    rec: dict[str, Any] = {"logins": [], "threads": [], "sent": [], "fail_with": None}

    @contextlib.contextmanager
    def fake_session(config: Any) -> Iterator[FakeIMAP]:
        rec["logins"].append((config.mail_user, config.mail_pass, config.imap_host))
        rec["threads"].append(threading.get_ident())
        if rec["fail_with"] is not None:
            raise rec["fail_with"](config)
        yield FakeIMAP()

    def fake_send(config: Any, msg: Any) -> str:
        rec["sent"].append((config.mail_user, config.mail_pass, msg["To"]))
        return "<mid@example.test>"

    monkeypatch.setattr(user_tools, "imap_session", fake_session)
    monkeypatch.setattr(user_tools, "send_via_submission", fake_send)
    monkeypatch.setattr(user_tools, "append_to_sent", lambda config, msg: True)
    return rec


# =============================================================================
# Env / server helpers
# =============================================================================


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def audit_log(tmp_path: Path) -> Path:
    return tmp_path / "audit.log"


_CLEAR = (
    "MAILCOW_HOST", "MAILCOW_MAIL_USER", "MAILCOW_MAIL_PASS", "MAILCOW_IMAP_HOST",
    "MAILCOW_SMTP_HOST", "MAILCOW_IMAP_PORT", "MAILCOW_SMTP_PORT",
    "MCP_MAILCOW_ENABLE_WRITE_TOOLS", "MCP_MAILCOW_ENABLE_MAIL_SEND",
    "MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS", "MCP_MAILCOW_TEMP_PASSWORD_TTL",
    "MCP_MAILCOW_MAILBOXES", "MCP_MAILCOW_MODE",
)


@pytest.fixture
def all_env(monkeypatch: pytest.MonkeyPatch, audit_log: Path) -> pytest.MonkeyPatch:
    for var in _CLEAR:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MAILCOW_ADMIN_URL", URL)
    monkeypatch.setenv("MAILCOW_ADMIN_API_KEY", API_KEY)
    monkeypatch.setenv("MCP_MAILCOW_MAILBOXES", f"{MB_A}, B@Example.TEST")
    monkeypatch.setenv("MCP_MAILCOW_AUDIT_LOG", str(audit_log))
    return monkeypatch


@pytest.fixture
def fake() -> Iterator[FakeMailcow]:
    f = FakeMailcow()
    with respx.mock(assert_all_called=False) as mock:
        f.install(mock)
        yield f


def _client(write: bool = False) -> TestClient:
    built = build_server("all", enable_write_tools=write)
    cfg = HttpConfig(host="127.0.0.1", port=0, auth_token=TOKEN)
    return TestClient(create_app(built, cfg))


def _rpc(client: TestClient, method: str, params: dict[str, Any] | None = None) -> Any:
    h = {"Accept": ACCEPT, "Content-Type": "application/json",
         "Authorization": f"Bearer {TOKEN}"}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = client.post("/mcp", content=json.dumps(body), headers=h)
    assert r.status_code == HTTPStatus.OK, r.text
    return r.json()["result"]


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> str:
    res = _rpc(client, "tools/call", {"name": name, "arguments": arguments})
    return "".join(c["text"] for c in res["content"])


def _audit(audit_log: Path) -> list[dict[str, Any]]:
    if not audit_log.exists():
        return []
    return [json.loads(x) for x in audit_log.read_text(encoding="utf-8").splitlines()]


# =============================================================================
# Schema constants
# =============================================================================


def test_policy_constants_name_real_tools(schema: dict[str, Any]) -> None:
    by_name = {t["name"]: t for t in schema["tools"]}
    assert len(by_name) == len(schema["tools"])  # names unique across modes
    assert SENSITIVE_TOOLS <= by_name.keys()
    assert all(by_name[n]["mode"] == "admin" for n in SENSITIVE_TOOLS)
    assert SEND_TOOLS <= by_name.keys()
    assert all(by_name[n]["mode"] == "user" for n in SEND_TOOLS)
    assert all("mailbox" not in (t.get("params") or {})
               for t in schema["tools"] if t["mode"] == "user")


def _expected_default(schema: dict[str, Any]) -> set[str]:
    return {
        t["name"] for t in schema["tools"]
        if t["readonly"] is True and t["name"] not in SENSITIVE_TOOLS
    }


def test_policy_counts(schema: dict[str, Any]) -> None:
    defs = tools_for_mode(schema, "all")
    assert len(defs) == ALL_TOOLS

    def n(w: bool, s: bool, hide: bool) -> int:
        return len(ToolPolicy(w, enable_mail_send=s, hide_sensitive=hide).visible_tools(defs))

    assert n(False, False, True) == DEFAULT_TOOLS
    assert n(True, False, True) == WRITE_TOOLS_ON
    assert n(False, True, True) == SEND_ON
    assert n(True, True, True) == WRITE_AND_SEND_ON
    assert n(True, True, False) == ALL_TOOLS
    assert n(False, False, False) == SENSITIVE_ALLOWED
    assert n(True, False, False) == WRITE_AND_SENSITIVE_ON


# =============================================================================
# Config / env
# =============================================================================


def test_allowlist_parsing() -> None:
    a = parse_mailbox_allowlist(" A@Example.Test ,, b@example.test ,")
    assert a == MailboxAllowList(frozenset({MB_A, MB_B}), allow_all=False)
    assert a.allows(MB_A) and not a.allows(MB_OUTSIDE)
    star = parse_mailbox_allowlist("*")
    assert star.allow_all and star.allows("anyone@x.test")
    mixed = parse_mailbox_allowlist(f"{MB_A}, *")
    assert mixed.allow_all and mixed.mailboxes == {MB_A}
    for bad in ("", " , ", "not-an-address", "a@b/../c", "a b@x.test", "a@x.test?x=1"):
        with pytest.raises(ValueError):
            parse_mailbox_allowlist(bad)


@pytest.mark.parametrize(
    "missing", ["MAILCOW_ADMIN_URL", "MAILCOW_ADMIN_API_KEY", "MCP_MAILCOW_MAILBOXES"]
)
def test_missing_env_exits_2_without_values(
    all_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    all_env.delenv(missing)
    with pytest.raises(SystemExit) as ei:
        build_server("all", enable_write_tools=False)
    assert ei.value.code == EXIT_USAGE
    err = capsys.readouterr().err
    assert missing in err
    assert API_KEY not in err and URL not in err and MB_A not in err


def test_invalid_allowlist_exits_2_without_value(
    all_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    all_env.setenv("MCP_MAILCOW_MAILBOXES", f"{MB_A}, secret-looking-value")
    with pytest.raises(SystemExit) as ei:
        load_all_config()
    assert ei.value.code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "MCP_MAILCOW_MAILBOXES" in err and "secret-looking-value" not in err


@pytest.mark.parametrize("ttl", ["abc", "0", "5", "100000"])
def test_invalid_ttl_exits_2(all_env: pytest.MonkeyPatch, ttl: str) -> None:
    all_env.setenv("MCP_MAILCOW_TEMP_PASSWORD_TTL", ttl)
    with pytest.raises(SystemExit) as ei:
        load_all_config()
    assert ei.value.code == EXIT_USAGE


def test_all_config_does_not_need_single_account_vars(all_env: pytest.MonkeyPatch) -> None:
    all_env.setenv("MAILCOW_MAIL_USER", "ignored@example.test")
    all_env.setenv("MAILCOW_MAIL_PASS", "ignored-password")
    cfg = load_all_config()
    assert cfg.host == "mail.example.test"  # from MAILCOW_ADMIN_URL
    assert cfg.imap_host == cfg.smtp_host == "mail.example.test"
    assert (cfg.imap_port, cfg.smtp_port) == (993, 587)
    assert cfg.temp_password_ttl == DEFAULT_TTL
    assert not cfg.enable_mail_send and not cfg.allow_sensitive_tools
    assert "ignored" not in repr(cfg)
    uc = cfg.user_config(MB_A, "pw-xyz-123456")
    assert uc.mail_user == MB_A and "pw-xyz" not in repr(uc)


def test_all_config_host_rules(all_env: pytest.MonkeyPatch) -> None:
    all_env.setenv("MAILCOW_HOST", "mx.example.test")
    all_env.setenv("MAILCOW_SMTP_HOST", "smtp.example.test")
    all_env.setenv("MAILCOW_SMTP_PORT", "465")
    cfg = load_all_config()
    assert (cfg.imap_host, cfg.smtp_host, cfg.smtp_port) == (
        "mx.example.test", "smtp.example.test", 465)


def test_write_default_off_in_all_mode_for_every_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_MAILCOW_ENABLE_WRITE_TOOLS", raising=False)
    assert write_tools_enabled("stdio", "all") is False
    assert write_tools_enabled("http", "all") is False
    assert write_tools_enabled("stdio", "admin") is True  # unchanged
    assert write_tools_enabled("stdio") is True
    monkeypatch.setenv("MCP_MAILCOW_ENABLE_WRITE_TOOLS", "true")
    assert write_tools_enabled("stdio", "all") is True


def test_cli_accepts_mode_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_MAILCOW_MODE", "ALL")
    assert env_mode() == "all"
    assert cli._build_parser().parse_args(["--mode", "all"]).mode == "all"


# =============================================================================
# tools/list over HTTP (also runs the lifespan: startup cleanup + shutdown)
# =============================================================================


def test_default_tools_list(all_env: pytest.MonkeyPatch, fake: FakeMailcow,
                            schema: dict[str, Any]) -> None:
    with _client() as c:
        tools = _rpc(c, "tools/list")["tools"]
    names = {t["name"] for t in tools}
    assert names == _expected_default(schema)
    assert len(names) == DEFAULT_TOOLS
    defs = {t["name"]: t for t in schema["tools"]}
    for t in tools:
        d = defs[t["name"]]
        if d["mode"] == "user":
            assert t["inputSchema"]["properties"]["mailbox"]["type"] == "string"
            assert "mailbox" in t["inputSchema"]["required"]
        else:  # admin tools: schema unchanged
            assert t["inputSchema"] == to_mcp_tool(d)["inputSchema"]


_VARIANTS = [
    (True, {}, WRITE_TOOLS_ON, {"domain_delete", "save_draft", "move_message"},
     SEND_TOOLS | SENSITIVE_TOOLS),
    (False, {"MCP_MAILCOW_ENABLE_MAIL_SEND": "true"}, SEND_ON, SEND_TOOLS,
     SENSITIVE_TOOLS | {"save_draft", "domain_delete"}),
    (True, {"MCP_MAILCOW_ENABLE_MAIL_SEND": "true",
            "MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS": "true"}, ALL_TOOLS,
     SEND_TOOLS | SENSITIVE_TOOLS, set()),
    (False, {"MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS": "true"}, SENSITIVE_ALLOWED,
     {"app_password_list"}, {"mailbox_set_password"}),
]


@pytest.mark.parametrize("case", _VARIANTS)
def test_tools_list_variants(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, case: tuple[Any, ...]
) -> None:
    write, env, count, present, absent = case
    for k, v in env.items():
        all_env.setenv(k, v)
    with _client(write=write) as c:
        names = {t["name"] for t in _rpc(c, "tools/list")["tools"]}
    assert len(names) == count
    assert present <= names
    assert not (absent & names)


# =============================================================================
# Refusals
# =============================================================================


def _api_ops(fake: FakeMailcow) -> list[str]:
    return [op for op, _ in fake.ops if op in ("add", "delete")]


def test_mailbox_outside_allowlist_refused_before_app_password(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any], audit_log: Path
) -> None:
    with _client() as c:
        text = _call(c, "list_folders", {"mailbox": MB_OUTSIDE})
    assert "not allowed" in text
    assert _api_ops(fake) == [] and mail_io["logins"] == []
    denied = [e for e in _audit(audit_log) if e["result"] == "denied"]
    assert denied[-1]["action"] == "list_folders"
    assert denied[-1]["mailbox"] == MB_OUTSIDE
    assert denied[-1]["kind"] == "user" and denied[-1]["mode"] == "all"


@pytest.mark.parametrize("bad", ["", "nobody", f"{MB_A}/../x", 5])
def test_invalid_mailbox_refused(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any], bad: Any
) -> None:
    with _client() as c:
        text = _call(c, "list_folders", {"mailbox": bad})
    assert "error" in text.lower()
    assert _api_ops(fake) == [] and mail_io["logins"] == []


def test_mailbox_argument_is_required(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any]
) -> None:
    with _client() as c:
        text = _call(c, "list_folders", {})
    assert "mailbox" in text and "required" in text  # jsonschema validation (1.x and 2.x)
    assert mail_io["logins"] == []


async def test_registry_handler_rejects_missing_mailbox(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any]
) -> None:
    cfg = load_all_config()
    registry, ctx = build_all_registry(cfg, AuditLogger(cfg.audit_log))
    with pytest.raises(AccessDenied):
        await registry["list_inbox"]({})
    with pytest.raises(AccessDenied):
        await registry["list_inbox"]({"mailbox": MB_OUTSIDE})
    assert mail_io["logins"] == [] and _api_ops(fake) == []
    await ctx.aclose()


def test_sensitive_tool_hidden_and_refused_even_with_write(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, audit_log: Path
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        fake.install(mock)
        route = mock.post(f"{URL}/api/v1/edit/mailbox").respond(json=[{"type": "success"}])
        with _client(write=True) as c:
            text = _call(c, "mailbox_set_password",
                         {"email": MB_A, "password": "Very-Secret-Pw1", "confirm": True})
        assert route.call_count == 0
    assert "MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS" in text
    entry = _audit(audit_log)[-1]
    assert entry["result"] == "denied" and entry["error"] == "sensitive tool hidden"
    assert entry["kind"] == "admin"
    assert "Very-Secret-Pw1" not in audit_log.read_text(encoding="utf-8")


def test_sensitive_tool_allowed_when_opted_in(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow
) -> None:
    all_env.setenv("MCP_MAILCOW_ALLOW_SENSITIVE_TOOLS", "true")
    with respx.mock(assert_all_called=False) as mock:
        fake.install(mock)
        route = mock.post(f"{URL}/api/v1/edit/mailbox").respond(json=[{"type": "success"}])
        with _client(write=True) as c:
            _call(c, "mailbox_set_password",
                  {"email": MB_A, "password": "Very-Secret-Pw1", "confirm": True})
        assert route.call_count == 1


def test_send_needs_mail_send_flag_not_write_flag(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any]
) -> None:
    args = {"mailbox": MB_A, "to": ["x@example.org"], "subject": "s", "body": "b"}
    with _client(write=True) as c:  # write on, send off
        assert "MCP_MAILCOW_ENABLE_MAIL_SEND" in _call(c, "send_message", args)
    assert mail_io["sent"] == []
    all_env.setenv("MCP_MAILCOW_ENABLE_MAIL_SEND", "true")
    with _client(write=False) as c:  # send on, write off
        text = _call(c, "send_message", args)
        assert '"sent": true' in text
        # other write tools stay gated by ENABLE_WRITE_TOOLS
        assert "MCP_MAILCOW_ENABLE_WRITE_TOOLS" in _call(
            c, "move_message", {"mailbox": MB_A, "uid": 1, "dest_folder": "X"})
    assert [s[0] for s in mail_io["sent"]] == [MB_A]


def test_write_param_on_read_tool_is_a_write(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any]
) -> None:
    with _client() as c:
        text = _call(c, "read_message", {"mailbox": MB_A, "uid": 1, "mark_read": True})
    assert "MCP_MAILCOW_ENABLE_WRITE_TOOLS" in text
    assert mail_io["logins"] == [] and _api_ops(fake) == []


# =============================================================================
# Routing and the app password lifecycle through the server
# =============================================================================


def test_routing_admin_vs_user(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any], audit_log: Path
) -> None:
    with _client() as c:
        text = _call(c, "domain_list", {})
        assert "example.test" in text
        assert _api_ops(fake) == [] and mail_io["logins"] == []  # admin: API only

        text = _call(c, "list_folders", {"mailbox": "B@example.test"})
        assert "INBOX" in text
        _call(c, "list_folders", {"mailbox": MB_B})  # cached: no second add
        assert [op for op in fake.ops if op[0] == "add"] == [("add", MB_B)]
        user, password, host = mail_io["logins"][0]
        assert user == MB_B and host == "mail.example.test"
        assert password == next(iter(fake.plain.values()))
        assert fake.names(MB_B)[0].startswith(TEMP_PREFIX)
    # shutdown (lifespan) deleted it
    assert fake.names(MB_B) == []
    assert _api_ops(fake)[-1] == "delete"
    log_text = audit_log.read_text(encoding="utf-8")
    assert password not in log_text
    kinds = {(e.get("kind"), e["action"]) for e in _audit(audit_log)}
    assert ("admin", "domain_list") in kinds and ("user", "list_folders") in kinds


def test_user_handler_runs_off_the_event_loop_thread(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any]
) -> None:
    with _client() as c:
        assert c.portal is not None
        loop_thread = c.portal.call(threading.get_ident)
        _call(c, "list_folders", {"mailbox": MB_A})
    assert mail_io["threads"] and mail_io["threads"][0] != loop_thread


def test_audit_has_metadata_but_no_content_or_password(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any],
    audit_log: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    all_env.setenv("MCP_MAILCOW_ENABLE_MAIL_SEND", "true")
    caplog.set_level(logging.DEBUG)
    args = {
        "mailbox": MB_A, "to": ["x@example.org", "y@example.org"], "cc": ["z@example.org"],
        "subject": "TOPSECRET-SUBJECT", "body": "TOPSECRET-BODY",
        "attachments": [{"filename": "f.txt", "mime_type": "text/plain",
                         "content_b64": "VE9QU0VDUkVULUFUVEFDSA=="}],
    }
    with _client() as c:
        out = _call(c, "send_message", args)
    password = next(iter(fake.plain.values()))
    log_text = audit_log.read_text(encoding="utf-8")
    for secret in ("TOPSECRET", "VE9QU0VDUkVULUFUVEFDSA", password, API_KEY):
        assert secret not in log_text
        assert secret not in caplog.text
    assert password not in out
    entry = next(e for e in _audit(audit_log) if e["action"] == "send_message")
    assert entry["mode"] == "all" and entry["kind"] == "user" and entry["mailbox"] == MB_A
    assert entry["params"] == {"to_count": 2, "cc_count": 1}
    actions = [e["action"] for e in _audit(audit_log)]
    assert "temp_app_password_create" in actions and "temp_app_password_delete" in actions


def test_backend_error_echoing_password_is_masked(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow, mail_io: dict[str, Any], audit_log: Path
) -> None:
    class Boom(Exception):
        def __init__(self, config: Any) -> None:
            super().__init__(f"LOGIN failed for {config.mail_user} {config.mail_pass}")

    mail_io["fail_with"] = Boom
    with _client() as c:
        text = _call(c, "list_folders", {"mailbox": MB_A})
    password = next(iter(fake.plain.values()))
    assert "LOGIN failed" in text and password not in text
    assert password not in audit_log.read_text(encoding="utf-8")


def test_sanitizer_masks_registered_secret() -> None:
    register_secret("Zx9-temp-secret-value")
    try:
        assert sanitize_text("oops Zx9-temp-secret-value!") == "oops ***!"
    finally:
        unregister_secret("Zx9-temp-secret-value")
    assert "Zx9" in sanitize_text("oops Zx9-temp-secret-value!")
    register_secret("short")  # too short: ignored
    assert sanitize_text("short") == "short"


# =============================================================================
# TempAppPasswordManager (unit, fake clock)
# =============================================================================


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def mgr(fake: FakeMailcow, tmp_path: Path) -> Iterator[tuple[TempAppPasswordManager, Clock]]:
    clock = Clock()
    client = MailcowClient(URL, API_KEY)
    m = TempAppPasswordManager(client, AuditLogger(tmp_path / "a.log"), ttl=600, clock=clock)
    yield m, clock


async def test_create_use_delete_order(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, _ = mgr
    async with m.lease(MB_A) as lease:
        assert lease.username == MB_A
        assert [op for op, _ in fake.ops] == ["add", "list"]
        used = lease.password
    assert fake.names(MB_A) and _api_ops(fake) == ["add"]  # cached, not deleted yet
    await m.aclose()
    assert [op for op, _ in fake.ops] == ["add", "list", "delete"]
    assert fake.names(MB_A) == []
    p = fake.add_payloads[0]
    assert p["username"] == MB_A and p["protocols"] == ["imap_access", "smtp_access"]
    assert p["app_name"].startswith(TEMP_PREFIX) and p["active"] == 1
    assert p["app_passwd"] == p["app_passwd2"] == used and len(used) >= MIN_PASSWORD_LEN
    assert used not in repr(lease)


async def test_ttl_expiry_deletes_and_rotates(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, clock = mgr
    async with m.lease(MB_A) as l1:
        first = l1.password
    clock.t += 601
    await m.sweep()
    assert fake.names(MB_A) == [] and _api_ops(fake) == ["add", "delete"]
    async with m.lease(MB_A) as l2:
        assert l2.password != first
    await m.aclose()


async def test_expired_lease_deleted_on_next_use(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, clock = mgr
    async with m.lease(MB_A):
        pass
    clock.t += 601
    async with m.lease(MB_A):
        assert _api_ops(fake) == ["add", "delete", "add"]
    await m.aclose()


async def test_expired_but_in_use_is_kept_until_released(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, clock = mgr
    async with m.lease(MB_A):
        clock.t += 601
        await m.sweep()
        assert _api_ops(fake) == ["add"]  # in use: not deleted
    assert _api_ops(fake) == ["add", "delete"]  # deleted on release
    await m.aclose()


async def test_shutdown_deletes_all_and_in_flight_on_release(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, _ = mgr
    async with m.lease(MB_A):
        pass
    async with m.lease(MB_B):
        await m.aclose()  # MB_A deleted now, MB_B still leased
        assert fake.names(MB_A) == [] and fake.names(MB_B)
    assert fake.names(MB_B) == []
    with pytest.raises(TempPasswordError):
        async with m.lease(MB_A):
            pass


async def test_concurrent_leases_create_once(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, _ = mgr

    async def use(mb: str) -> str:
        async with m.lease(mb) as lease:
            await asyncio.sleep(0.01)
            return lease.password

    results = await asyncio.gather(*[use(MB_A) for _ in range(10)], use(MB_B), use(MB_B))
    assert len(set(results[:10])) == 1
    assert sorted(mb for op, mb in fake.ops if op == "add") == [MB_A, MB_B]
    assert len(fake.names(MB_A)) == 1
    await m.aclose()


async def test_create_failure_raises_without_secret(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, _ = mgr
    fake.fail_add = True
    with pytest.raises(TempPasswordError) as ei:
        async with m.lease(MB_A):
            pass
    sent = fake.add_payloads[0]["app_passwd"]
    assert sent not in str(ei.value)
    assert sanitize_text(f"x {sent}") == f"x {sent}"  # unregistered after failure
    await m.aclose()


async def test_startup_cleanup_only_deletes_prefixed(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, _ = mgr
    fake.seed(MB_A, "mcp-temp-1700000000-dead")
    fake.seed(MB_A, "Thunderbird")
    fake.seed(MB_A, "mcp-tempfoo")  # no dash after prefix: not ours
    fake.seed(MB_A, "my-mcp-temp-x")
    fake.seed(MB_OUTSIDE, "mcp-temp-other")  # not in allow-list
    deleted = await m.cleanup_leftovers(parse_mailbox_allowlist(f"{MB_A},{MB_B}"))
    assert deleted == 1
    assert fake.names(MB_A) == ["Thunderbird", "mcp-tempfoo", "my-mcp-temp-x"]
    assert fake.names(MB_OUTSIDE) == ["mcp-temp-other"]
    assert ("mailbox_list", None) not in fake.ops


async def test_startup_cleanup_star_uses_mailbox_list(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow
) -> None:
    m, _ = mgr
    fake.seed(MB_OUTSIDE, "mcp-temp-other")
    fake.seed(MB_OUTSIDE, "Phone")
    assert await m.cleanup_leftovers(parse_mailbox_allowlist("*")) == 1
    assert fake.names(MB_OUTSIDE) == ["Phone"]


async def test_startup_cleanup_failure_only_warns(
    mgr: tuple[TempAppPasswordManager, Clock], fake: FakeMailcow,
    caplog: pytest.LogCaptureFixture,
) -> None:
    m, _ = mgr
    fake.fail_mailbox_list = True
    with caplog.at_level(logging.WARNING):
        assert await m.cleanup_leftovers(parse_mailbox_allowlist("*")) == 0
    assert "could not list mailboxes" in caplog.text
    assert API_KEY not in caplog.text


def test_server_starts_even_if_cleanup_fails(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow
) -> None:
    all_env.setenv("MCP_MAILCOW_MAILBOXES", "*")
    fake.fail_mailbox_list = True
    with _client() as c:
        assert c.get("/health").status_code == HTTPStatus.OK
        assert len(_rpc(c, "tools/list")["tools"]) == DEFAULT_TOOLS


def test_startup_cleanup_runs_in_lifespan(
    all_env: pytest.MonkeyPatch, fake: FakeMailcow
) -> None:
    fake.seed(MB_A, "mcp-temp-leftover")
    fake.seed(MB_A, "Keep-me")
    with _client() as c:
        assert c.get("/health").status_code == HTTPStatus.OK
        assert fake.names(MB_A) == ["Keep-me"]
