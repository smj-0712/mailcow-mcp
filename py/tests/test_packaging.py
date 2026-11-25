"""Repo-level consistency checks for files that must stay in sync."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = REPO_ROOT / "py"

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    tomllib = pytest.importorskip("tomli")


def _requirements() -> list[str]:
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _pyproject_deps() -> list[str]:
    with (PY / "pyproject.toml").open("rb") as f:
        deps: list[str] = tomllib.load(f)["project"]["dependencies"]
    return deps


def test_requirements_txt_matches_pyproject() -> None:
    assert _requirements() == _pyproject_deps()


def test_mcp_range_covers_1x_and_2x() -> None:
    (mcp,) = [d for d in _pyproject_deps() if d.startswith("mcp")]
    assert mcp == "mcp>=1.10.0,<3"


def test_readmes_identical() -> None:
    assert (REPO_ROOT / "README.md").read_bytes() == (PY / "README.md").read_bytes()


def test_tools_schema_copies_identical() -> None:
    root = (REPO_ROOT / "tools-schema.yaml").read_bytes()
    assert root == (PY / "src" / "mcp_mailcow" / "tools-schema.yaml").read_bytes()
