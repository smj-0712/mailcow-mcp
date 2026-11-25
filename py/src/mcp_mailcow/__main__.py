"""Entry point: ``mcp-mailcow [--mode user|admin|all] [--transport stdio|http]``.

Every flag has an environment-variable default (``MCP_MAILCOW_MODE``,
``MCP_MAILCOW_TRANSPORT``, ...). ``main()`` can also load a ``.env`` file first
(the repo-root ``main.py`` does), which never overrides real environment
variables. Precedence: CLI flag > environment variable > .env > default.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import NoReturn

from . import __version__
from .config import (
    MODES,
    TRANSPORTS,
    env_host,
    env_mode,
    env_port,
    env_transport,
    load_http_config,
    write_tools_enabled,
)
from .dotenv import load_dotenv
from .http_app import run_http
from .server import build_server, run_stdio


def _version_string() -> str:
    """Read the installed package version. Falls back to the source tree's
    ``__version__`` when running from a checkout that hasn't been installed
    (e.g. ``python3 main.py`` or ``python -m mcp_mailcow``)."""
    try:
        return _pkg_version("mcp-mailcow")
    except PackageNotFoundError:
        return __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-mailcow",
        description="MCP server for Mailcow (mail + admin)",
    )
    parser.add_argument(
        "--mode",
        choices=list(MODES),
        default=env_mode(),
        help="user: IMAP/SMTP mailbox operations | admin: Mailcow REST API operations | "
        "all: admin tools plus user tools for the mailboxes in MCP_MAILCOW_MAILBOXES. "
        "Default from MCP_MAILCOW_MODE; one of them is required.",
    )
    parser.add_argument(
        "--transport",
        choices=list(TRANSPORTS),
        default=env_transport(),
        help="stdio (default) or http (Streamable HTTP on /mcp). "
        "Default from MCP_MAILCOW_TRANSPORT.",
    )
    parser.add_argument(
        "--host",
        default=env_host(),
        help="HTTP bind address (default: MCP_MAILCOW_HOST or 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_port(),
        help="HTTP port (default: MCP_MAILCOW_PORT, else SERVER_PORT, else 8000)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_version_string()}",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, dotenv_path: Path | None = None) -> NoReturn:
    """Run the server. ``dotenv_path``: optional ``.env`` file loaded before
    reading any setting (missing file is fine; existing env vars win)."""
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.mode is None:
        parser.error(
            "the server mode is required: pass --mode user|admin|all or set "
            "MCP_MAILCOW_MODE=user|admin|all (environment variable or .env)"
        )

    if args.transport == "stdio":
        built = build_server(args.mode, enable_write_tools=write_tools_enabled("stdio", args.mode))
        try:
            asyncio.run(run_stdio(built))
        except KeyboardInterrupt:
            sys.exit(0)
        sys.exit(0)

    # --- HTTP ---------------------------------------------------------------
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Check auth before touching the Mailcow config: refuse early, exit 2.
    http_cfg = load_http_config(args.host, args.port)
    built = build_server(args.mode, enable_write_tools=write_tools_enabled("http", args.mode))
    try:
        run_http(built, http_cfg)
    except KeyboardInterrupt:
        sys.exit(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
