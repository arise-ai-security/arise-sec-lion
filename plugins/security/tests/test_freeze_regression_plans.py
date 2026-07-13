"""Regression-plan generator invariants for the confirmatory security cohort."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from experiments.shared.evaluation.regression import RegressionCommand
from plugins.security.scripts.freeze_regression_plans import _PROJECT_PROBES


def test_confirmatory_projects_have_nontrivial_probes() -> None:
    root = Path(__file__).resolve().parents[3]
    dataset = yaml.safe_load(
        (root / "experiments/b4-confirmatory-cohort/dataset.yaml").read_text(
            encoding="utf-8"
        )
    )
    projects = {
        json.loads((root / path).read_text(encoding="utf-8"))["project_name"]
        for path in dataset["source"]["paths"]
    }

    assert projects == set(_PROJECT_PROBES)
    for argv, timeout in _PROJECT_PROBES.values():
        RegressionCommand(argv=argv, timeout_seconds=timeout)
