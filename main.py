#!/usr/bin/env python3
"""Run mcp-mailcow from a source checkout, without uv/uvx or `pip install .`.

    pip install -r requirements.txt
    cp .env.example .env   # then edit .env
    python main.py

Puts ``py/src`` on ``sys.path`` and calls ``mcp_mailcow.__main__.main()``,
loading ``.env`` from this file's folder first (real environment variables
and CLI flags take precedence over it). Every CLI flag still works, e.g.
``python main.py --mode admin --transport http``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "py" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp_mailcow.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main(dotenv_path=ROOT / ".env")
