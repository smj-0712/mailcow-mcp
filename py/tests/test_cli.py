"""CLI / startup behaviour: transport options, env defaults, auth refusal."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from mcp_mailcow import __main__ as cli
from mcp_mailcow import __version__, http_app
from mcp_mailcow.config import write_tools_enabled

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "cli-test-token-9876543210"
API_KEY = "cli-dummy-api-key-555"
EXIT_USAGE = 2  # config.py exits with 2 on bad/missing settings
PORT = 8123

_ENV_VARS = (
    "MCP_MAILCOW_TRANSPORT", "MCP_MAILCOW_HOST", "MCP_MAILCOW_PORT",
    "MCP_MAILCOW_AUTH_TOKEN", "MCP_MAILCOW_ALLOW_NO_AUTH",
    "MCP_MAILCOW_ENABLE_WRITE_TOOLS", "MCP_MAILCOW_ALLOWED_HOSTS",
    "MCP_MAILCOW_ALLOWED_ORIGINS", "MCP_MAILCOW_MODE", "SERVER_PORT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MAILCOW_ADMIN_URL", "https://mail.example.test")
    monkeypatch.setenv("MAILCOW_ADMIN_API_KEY", API_KEY)
    monkeypatch.setenv("MCP_MAILCOW_AUDIT_LOG", str(tmp_path / "audit.log"))


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def run(app: Any, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(http_app.uvicorn, "run", run)
    return calls


def _main(*argv: str) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    return int(exc.value.code or 0)


def test_transport_defaults_are_stdio_0000_8000() -> None:
    args = cli._build_parser().parse_args(["--mode", "admin"])
    assert (args.transport, args.host, args.port) == ("stdio", "0.0.0.0", 8000)


def test_transport_defaults_come_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_MAILCOW_TRANSPORT", "HTTP")
    monkeypatch.setenv("MCP_MAILCOW_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_MAILCOW_PORT", "9123")
    args = cli._build_parser().parse_args(["--mode", "admin"])
    assert (args.transport, args.host, args.port) == ("http", "127.0.0.1", 9123)
    # explicit flags win over env
    args = cli._build_parser().parse_args(["--mode", "admin", "--transport", "stdio",
                                           "--port", "8001"])
    assert (args.transport, args.port) == ("stdio", 8001)


@pytest.mark.parametrize(("var", "value"), [
    ("MCP_MAILCOW_PORT", "eighty"), ("MCP_MAILCOW_PORT", "70000"),
    ("MCP_MAILCOW_TRANSPORT", "sse"),
])
def test_invalid_env_defaults_exit_2(monkeypatch: pytest.MonkeyPatch, var: str,
                                     value: str) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(SystemExit) as exc:
        cli._build_parser()
    assert exc.value.code == EXIT_USAGE


def test_http_without_token_refuses_to_start(
    fake_uvicorn: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main("--mode", "admin", "--transport", "http") == EXIT_USAGE
    assert fake_uvicorn == []
    assert "MCP_MAILCOW_AUTH_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["false", "0", "", "maybe"])
def test_allow_no_auth_must_be_explicitly_true(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: list[dict[str, Any]], value: str
) -> None:
    monkeypatch.setenv("MCP_MAILCOW_ALLOW_NO_AUTH", value)
    assert _main("--mode", "admin", "--transport", "http") == EXIT_USAGE
    assert fake_uvicorn == []


def test_http_allow_no_auth_starts_with_warning(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MCP_MAILCOW_ALLOW_NO_AUTH", "true")
    caplog.set_level(logging.INFO)
    assert _main("--mode", "admin", "--transport", "http", "--port", str(PORT)) == 0
    assert len(fake_uvicorn) == 1
    assert fake_uvicorn[0]["port"] == PORT
    assert "AUTHENTICATION DISABLED" in caplog.text


def test_http_with_token_starts_and_never_logs_secrets(
    monkeypatch: pytest.MonkeyPatch, fake_uvicorn: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MCP_MAILCOW_AUTH_TOKEN", f"  {TOKEN}  ")
    caplog.set_level(logging.DEBUG)
    assert _main("--mode", "admin", "--transport", "http") == 0
    assert (fake_uvicorn[0]["host"], fake_uvicorn[0]["port"]) == ("0.0.0.0", 8000)
    assert "bearer token authentication enabled" in caplog.text
    assert "write tools disabled" in caplog.text
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert TOKEN not in text
        assert API_KEY not in text


def test_write_tools_default_per_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    assert write_tools_enabled("stdio") is True
    assert write_tools_enabled("http") is False
    monkeypatch.setenv("MCP_MAILCOW_ENABLE_WRITE_TOOLS", "true")
    assert write_tools_enabled("http") is True
    monkeypatch.setenv("MCP_MAILCOW_ENABLE_WRITE_TOOLS", "false")
    assert write_tools_enabled("stdio") is False


def test_root_main_py_runs_from_checkout() -> None:
    """`python3 main.py` must work without installing the package."""
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--version"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    assert out.stdout.strip() == f"mcp-mailcow {__version__}"
