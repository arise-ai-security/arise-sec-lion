"""Shared fixtures for harness integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from experiments.shared.scripts import _paths


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute REPO_ROOT to a clean tmp dir for isolated harness tests."""
    monkeypatch.setattr(_paths, "REPO_ROOT", tmp_path)
    return tmp_path
