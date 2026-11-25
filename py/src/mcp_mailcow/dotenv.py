"""Minimal ``.env`` loader (standard library only).

Supported syntax, one assignment per line::

    # full-line comment
    KEY=value
    KEY=value   # inline comment (unquoted values only; needs a space before #)
    KEY="double quoted value"   # quotes are removed, '#' inside is kept
    KEY='single quoted value'

Nothing else (no ``export``, no variable expansion, no escape sequences, no
multi-line values). Variables already present in the process environment are
never overwritten — even when set to an empty string — so the effective
precedence is: CLI flag > real environment variable > ``.env`` > default.

Values are never printed: warnings name the file and line number only.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INLINE_COMMENT = re.compile(r"\s+#.*$")


def _warn(msg: str) -> None:
    sys.stderr.write(f"warning: {msg}\n")


def _parse_value(raw: str) -> str | None:
    """Return the value, or None if the quoting is malformed."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = raw.find(quote, 1)
        if end == -1:
            return None
        rest = raw[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            return None
        return raw[1:end]
    if raw.startswith("#"):  # `KEY= # comment` -> empty value
        return ""
    return _INLINE_COMMENT.sub("", raw)


def parse_dotenv(text: str, source: str = ".env") -> dict[str, str]:
    """Parse ``.env`` text. Malformed lines are skipped with a warning."""
    result: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, raw = stripped.partition("=")
        key = key.strip()
        value = _parse_value(raw) if sep else None
        if not _KEY.match(key) or value is None:
            _warn(f"{source}:{lineno}: ignored (expected KEY=VALUE)")
            continue
        result[key] = value
    return result


def load_dotenv(
    path: Path, environ: MutableMapping[str, str] | None = None
) -> list[str]:
    """Load ``path`` into ``environ`` (default ``os.environ``) without
    overriding existing keys. A missing file is not an error.

    Returns the names (never the values) of the variables that were set.
    """
    env = os.environ if environ is None else environ
    try:
        # utf-8-sig: tolerate the BOM that Windows editors like to add.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        _warn(f"could not read {path.name}: {type(e).__name__}; ignoring it")
        return []
    loaded: list[str] = []
    for key, value in parse_dotenv(text, source=path.name).items():
        if key not in env:
            env[key] = value
            loaded.append(key)
    return loaded
