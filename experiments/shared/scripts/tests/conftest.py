"""Shared fixtures for scripts-first writer/validator tests.

Every test runs with `REPO_ROOT` pointed at a pytest-managed tmp dir so
writes and validations never touch the real repository.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from experiments.shared.scripts import _paths


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reroute REPO_ROOT to a tmp dir and provision a `study/reports/` tree."""
    study_reports = tmp_path / "experiments" / "2026-01-01-fake-study" / "reports"
    study_reports.mkdir(parents=True)
    monkeypatch.setattr(_paths, "REPO_ROOT", tmp_path)
    return tmp_path
