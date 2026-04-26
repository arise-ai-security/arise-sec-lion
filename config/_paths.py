"""Resolve the repository root from the location of this file.

Resolves the parent of ``config/_paths.py`` (i.e. ``config/``'s parent) and
verifies it contains ``pyproject.toml``. Tests can patch ``REPO_ROOT`` via
``monkeypatch.setattr`` on this module.
"""

from __future__ import annotations

from pathlib import Path


def _find_repo_root() -> Path:
    here = Path(__file__).resolve().parent
    candidate = here.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    raise RuntimeError(
        f"Could not locate repository root from {here}: pyproject.toml not found in {candidate}"
    )


REPO_ROOT: Path = _find_repo_root()


def get_repo_root() -> Path:
    """Return the resolved repository root."""
    return REPO_ROOT
