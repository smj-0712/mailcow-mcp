"""`.env` loading, optional --mode, setting precedence and SERVER_PORT fallback."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from mcp_mailcow import __main__ as cli
from mcp_mailcow import http_app
from mcp_mailcow.dotenv import load_dotenv, parse_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "dotenv-test-token-ABCDEF123456"
API_KEY = "dotenv-dummy-api-key-777"
EXIT_USAGE = 2
FLAG_PORT, ENV_PORT, DOTENV_PORT, PTERO_PORT = 7003, 7002, 7001, 25565

_VARS = (
    "MCP_MAILCOW_MODE", "MCP_MAILCOW_TRANSPORT", "MCP_MAILCOW_HOST", "MCP_MAILCOW_PORT",
    "SERVER_PORT", "MCP_MAILCOW_AUTH_TOKEN", "MCP_MAILCOW_ALLOW_NO_AUTH",
    "MCP_MAILCOW_ENABLE_WRITE_TOOLS", "MCP_MAILCOW_ALLOWED_HOSTS",
    "MCP_MAILCOW_ALLOWED_ORIGINS", "MAILCOW_ADMIN_URL", "MAILCOW_ADMIN_API_KEY",
    "MCP_MAILCOW_AUDIT_LOG",
)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path) -> Iterator[None]:
    """load_dotenv writes straight into os.environ, so snapshot and restore it."""
    saved = dict(os.environ)
    for var in _VARS:
        os.environ.pop(var, None)
    os.environ["MCP_MAILCOW_AUDIT_LOG"] = str(tmp_path / "audit.log")
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(http_app.uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def _write_env(tmp_path: Path, text: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def _main(*argv: str, dotenv_path: Path | None = None) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv), dotenv_path=dotenv_path)
    return int(exc.value.code or 0)


ADMIN_DOTENV = f"""\
# dummy admin config
MCP_MAILCOW_MODE=admin
MCP_MAILCOW_TRANSPORT=http
MCP_MAILCOW_PORT={DOTENV_PORT}
MCP_MAILCOW_AUTH_TOKEN="{TOKEN}"
MAILCOW_ADMIN_URL=https://mail.example.test
MAILCOW_ADMIN_API_KEY='{API_KEY}'
"""


# --- parser ------------------------------------------------------------------


def test_parse_dotenv_syntax() -> None:
    text = (
        "﻿# comment\n\n"
        "A=plain\n"
        "B = spaced value  \n"
        'C="quoted # not a comment"\n'
        "D='single'  # trailing comment\n"
        "E=value # inline comment\n"
        "F=has#hash\n"
        "G=\n"
        "H= # only a comment\n"
        "I=a=b=c\r\n"
    )
    assert parse_dotenv(text.lstrip("﻿")) == {
        "A": "plain", "B": "spaced value", "C": "quoted # not a comment", "D": "single",
        "E": "value", "F": "has#hash", "G": "", "H": "", "I": "a=b=c",
    }


def test_malformed_lines_skipped_without_echoing_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "do-not-print-me-123"
    text = f"NOEQUALS {secret}\n1BAD={secret}\nQ=\"{secret}\nOK=1\nR='{secret}' junk\n"
    assert parse_dotenv(text) == {"OK": "1"}
    err = capsys.readouterr().err
    assert secret not in err
    assert err.count("ignored") == 4  # noqa: PLR2004


def test_load_dotenv_never_overrides_existing(tmp_path: Path) -> None:
    env = {"A": "from-env", "EMPTY": ""}
    loaded = load_dotenv(_write_env(tmp_path, "A=from-file\nEMPTY=x\nB=new\n"), env)
    assert loaded == ["B"]
    assert env == {"A": "from-env", "EMPTY": "", "B": "new"}


def test_load_dotenv_missing_file_is_fine(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert load_dotenv(tmp_path / "does-not-exist.env", env) == []
    assert env == {}


def test_load_dotenv_handles_bom(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_bytes("﻿MCP_MAILCOW_MODE=user\n".encode())
    env: dict[str, str] = {}
    load_dotenv(p, env)
    assert env == {"MCP_MAILCOW_MODE": "user"}


# --- --mode optional -----------------------------------------------------------


def test_mode_from_env_without_flag(fake_uvicorn: list[dict[str, Any]]) -> None:
    os.environ.update({
        "MCP_MAILCOW_MODE": "admin", "MCP_MAILCOW_AUTH_TOKEN": TOKEN,
        "MAILCOW_ADMIN_URL": "https://mail.example.test", "MAILCOW_ADMIN_API_KEY": API_KEY,
    })
    assert _main("--transport", "http") == 0
    assert len(fake_uvicorn) == 1


def test_no_mode_anywhere_exits_2(
    fake_uvicorn: list[dict[str, Any]], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert _main("--transport", "http", dotenv_path=tmp_path / "missing.env") == EXIT_USAGE
    err = capsys.readouterr().err
    assert "MCP_MAILCOW_MODE" in err and "--mode" in err
    assert fake_uvicorn == []


def test_invalid_mode_env_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    os.environ["MCP_MAILCOW_MODE"] = "superuser"
    assert _main() == EXIT_USAGE
    assert "MCP_MAILCOW_MODE must be one of user, admin" in capsys.readouterr().err


def test_mode_flag_still_works_and_beats_env() -> None:
    os.environ["MCP_MAILCOW_MODE"] = "user"
    assert cli._build_parser().parse_args(["--mode", "admin"]).mode == "admin"
    assert cli._build_parser().parse_args([]).mode == "user"


# --- precedence: flag > env > .env > default ----------------------------------


def test_dotenv_alone_starts_server(
    fake_uvicorn: list[dict[str, Any]], tmp_path: Path,
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    assert _main(dotenv_path=_write_env(tmp_path, ADMIN_DOTENV)) == 0
    assert fake_uvicorn[0]["port"] == DOTENV_PORT
    out = capsys.readouterr()
    for text in (caplog.text, out.out, out.err):
        assert TOKEN not in text
        assert API_KEY not in text


def test_env_beats_dotenv(fake_uvicorn: list[dict[str, Any]], tmp_path: Path) -> None:
    os.environ["MCP_MAILCOW_PORT"] = str(ENV_PORT)
    assert _main(dotenv_path=_write_env(tmp_path, ADMIN_DOTENV)) == 0
    assert fake_uvicorn[0]["port"] == ENV_PORT


def test_flag_beats_env_and_dotenv(fake_uvicorn: list[dict[str, Any]], tmp_path: Path) -> None:
    os.environ["MCP_MAILCOW_PORT"] = str(ENV_PORT)
    path = _write_env(tmp_path, ADMIN_DOTENV)
    assert _main("--port", str(FLAG_PORT), dotenv_path=path) == 0
    assert fake_uvicorn[0]["port"] == FLAG_PORT


def test_empty_env_var_is_not_overridden_by_dotenv(
    fake_uvicorn: list[dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    os.environ["MCP_MAILCOW_AUTH_TOKEN"] = ""  # e.g. an empty panel variable
    assert _main(dotenv_path=_write_env(tmp_path, ADMIN_DOTENV)) == EXIT_USAGE
    assert "MCP_MAILCOW_AUTH_TOKEN" in capsys.readouterr().err
    assert fake_uvicorn == []


def test_without_dotenv_file_env_only(fake_uvicorn: list[dict[str, Any]], tmp_path: Path) -> None:
    os.environ.update({
        "MCP_MAILCOW_MODE": "admin", "MCP_MAILCOW_TRANSPORT": "http",
        "MCP_MAILCOW_AUTH_TOKEN": TOKEN,
        "MAILCOW_ADMIN_URL": "https://mail.example.test", "MAILCOW_ADMIN_API_KEY": API_KEY,
    })
    assert _main(dotenv_path=tmp_path / "no-such.env") == 0
    assert fake_uvicorn[0]["port"] == 8000  # noqa: PLR2004 — built-in default


# --- SERVER_PORT (Pterodactyl) fallback ------------------------------------------


def test_server_port_used_when_mcp_port_unset() -> None:
    os.environ["SERVER_PORT"] = str(PTERO_PORT)
    assert cli._build_parser().parse_args([]).port == PTERO_PORT


def test_mcp_port_beats_server_port() -> None:
    os.environ["SERVER_PORT"] = str(PTERO_PORT)
    os.environ["MCP_MAILCOW_PORT"] = str(ENV_PORT)
    assert cli._build_parser().parse_args([]).port == ENV_PORT


def test_invalid_server_port_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    os.environ["SERVER_PORT"] = "not-a-port"
    with pytest.raises(SystemExit) as exc:
        cli._build_parser()
    assert exc.value.code == EXIT_USAGE
    assert "SERVER_PORT must be" in capsys.readouterr().err


# --- root main.py ----------------------------------------------------------------


def test_root_main_py_without_mode_exits_2() -> None:
    # An empty-but-set MCP_MAILCOW_MODE keeps a developer's real repo-root .env
    # (if any) from supplying the mode, so this is deterministic.
    env = {k: v for k, v in os.environ.items() if k not in _VARS}
    env["MCP_MAILCOW_MODE"] = ""
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py")],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    assert out.returncode == EXIT_USAGE
    assert "MCP_MAILCOW_MODE" in out.stderr
