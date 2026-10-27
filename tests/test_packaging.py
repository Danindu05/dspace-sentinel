"""``pyproject.toml`` and the requirements files pin the same versions.

``sentinel.sh`` and CI install from ``requirements*.txt``; ``pip install .[dev]`` reads
``pyproject.toml``. Both must agree, or the two ways of installing test different tools (a ruff
outside ``<0.17`` formats the tree differently from CI's ``ruff format --check``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    tomllib = pytest.importorskip("tomli")

REPO = Path(__file__).resolve().parents[1]


def _requirements(name: str) -> set[str]:
    """The requirement specifiers of ``name`` (comments, blank lines and ``-r`` includes dropped)."""
    lines = (line.split("#", 1)[0].strip() for line in (REPO / name).read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line and not line.startswith("-r")}


def test_pyproject_matches_the_requirements_files() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert set(project["dependencies"]) == _requirements("requirements.txt")
    assert set(project["optional-dependencies"]["dev"]) == _requirements("requirements-dev.txt")
