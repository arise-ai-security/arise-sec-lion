"""Tests for `experiments.shared.scripts.load_runs`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from experiments.shared.scripts.load_runs import load_runs


if TYPE_CHECKING:
    from pathlib import Path


def _write_manifest(
    runs_pool: Path,
    run_id: str,
    *,
    study_id: str | None = None,
    cell: str | None = None,
    task: str | None = None,
    kind: str = "ours",
    exit_status: str = "success",
) -> Path:
    run_dir = runs_pool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "kind": kind,
        "exit_status": exit_status,
    }
    if study_id is not None:
        payload["study_id"] = study_id
    if cell is not None:
        payload["cell"] = cell
    if task is not None:
        payload["task"] = task
    path = run_dir / "run_manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def test_load_runs_returns_every_manifest_when_no_filter(repo_root: Path) -> None:
    runs_pool = repo_root / "runs"
    _write_manifest(runs_pool, "run-1")
    _write_manifest(runs_pool, "run-2")

    rows = load_runs(output_directory=runs_pool)
    ids = sorted(row["run_id"] for row in rows)
    assert ids == ["run-1", "run-2"]


def test_load_runs_filters_by_study_and_cell(repo_root: Path) -> None:
    runs_pool = repo_root / "runs"
    _write_manifest(runs_pool, "keep", study_id="S", cell="A1", task="cve-a")
    _write_manifest(runs_pool, "drop-cell", study_id="S", cell="B2", task="cve-a")
    _write_manifest(runs_pool, "drop-study", study_id="OTHER", cell="A1", task="cve-a")

    rows = load_runs(
        output_directory=runs_pool,
        study_id="S",
        cells=["A1"],
    )
    assert [row["run_id"] for row in rows] == ["keep"]


