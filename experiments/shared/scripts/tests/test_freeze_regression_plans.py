"""Regression-plan generator invariants."""

from __future__ import annotations

import pytest

from experiments.shared.evaluation.regression import RegressionCommand
from experiments.shared.scripts.freeze_regression_plans import _PROJECT_PROBES, main


def test_project_probes_are_valid_regression_commands() -> None:
    assert _PROJECT_PROBES
    for argv, timeout in _PROJECT_PROBES.values():
        RegressionCommand(argv=argv, timeout_seconds=timeout)


def test_study_path_is_required() -> None:
    with pytest.raises(SystemExit):
        main([])
