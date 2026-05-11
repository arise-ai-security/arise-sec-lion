"""Tests for `experiments.shared.scripts.register_run`.

PR 1 of the experiments rearchitecture: ``register_run`` only stamps the
per-run ``run_manifest.json`` — the study manifest is design-only and
the enrollment roster lives in ``reports/enrollment.lock.yaml``. Tests
that previously asserted study-manifest writeback now assert the
manifest stays untouched.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import yaml

from experiments.shared.scripts.register_run import register_run


if TYPE_CHECKING:
    from pathlib import Path


def _write_study_manifest(repo_root: Path, study_id: str, cells: dict[str, dict]) -> Path:
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    manifest = study_dir / "manifest.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "schema_version": 2,
                "created_at": "2026-04-22T00:00:00Z",
                "cells": cells,
            },
            sort_keys=False,
        )
    )
    return manifest


def _seed_run_manifest(runs_pool: Path, run_id: str, extra: dict | None = None) -> Path:
    run_dir = runs_pool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run_manifest.json"
    payload = {
        "run_id": run_id,
        "kind": "ours",
        "exit_status": "success",
        "models": {"boss": "o3", "manager": "o3", "worker": "openai/o3"},
    }
    if extra:
        payload.update(extra)
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return manifest


def test_register_run_stamps_run_manifest_with_replicate(repo_root: Path) -> None:
    # Given: a study manifest declaring cell B2, and a finished run in the pool.
    study_id = "2026-04-22-stamp-test"
    _write_study_manifest(
        repo_root,
        study_id,
        cells={"B2": {"config": "configs/B2.yaml", "harness": "ours"}},
    )
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)
    run_id = uuid4()
    run_manifest = _seed_run_manifest(runs_pool, str(run_id))

    # When
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell="B2",
        task="gpac.cve-2021-40575",
        replicate=0,
        output_directory=runs_pool,
    )

    # Then: the run manifest now carries experiment-level fields.
    updated = json.loads(run_manifest.read_text())
    assert updated["study_id"] == study_id
    assert updated["cell"] == "B2"
    assert updated["task"] == "gpac.cve-2021-40575"
    assert updated["replicate"] == 0
    # And: the legacy attempt alias is preserved for one cycle.
    assert updated["attempt"] == 0
    # And: projection_status defaults to "ok" (audit N-2).
    assert updated["projection_status"] == "ok"


def test_register_run_stamps_projection_failed_when_passed(repo_root: Path) -> None:
    # Given: a finished run whose event projection raised in the harness.
    study_id = "2026-04-22-failed-projection"
    _write_study_manifest(
        repo_root,
        study_id,
        cells={"A1": {"config": "configs/A1.yaml", "harness": "ours"}},
    )
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)
    run_id = uuid4()
    run_manifest = _seed_run_manifest(runs_pool, str(run_id))

    # When: register_run is called with projection_status="failed"
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell="A1",
        task="t",
        replicate=0,
        output_directory=runs_pool,
        projection_status="failed",
    )

    # Then: the field surfaces in the manifest so collect.py can exclude it.
    updated = json.loads(run_manifest.read_text())
    assert updated["projection_status"] == "failed"


def test_register_run_accepts_legacy_attempt_kwarg(repo_root: Path) -> None:
    # Given: a study + run.
    study_id = "2026-04-22-attempt-alias"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}})
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    run_manifest = _seed_run_manifest(runs_pool, str(run_id))

    # When: caller still uses the old `attempt=` keyword.
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell="A1",
        task="t",
        attempt=2,
        output_directory=runs_pool,
    )

    # Then: replicate is set to the same value, and attempt is preserved.
    updated = json.loads(run_manifest.read_text())
    assert updated["replicate"] == 2
    assert updated["attempt"] == 2


def test_register_run_replicate_and_attempt_both_equal_succeeds(repo_root: Path) -> None:
    # Given: a study + run.
    study_id = "both-equal-study"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}})
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    run_manifest = _seed_run_manifest(runs_pool, str(run_id))

    # When: caller passes both `replicate=` and the legacy `attempt=` with the
    # same value (a transitional callsite that uses both names interchangeably).
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell="A1",
        task="t",
        replicate=2,
        attempt=2,
        output_directory=runs_pool,
    )

    # Then: no error raised, and replicate is stamped with the agreed value.
    payload = json.loads(run_manifest.read_text())
    assert payload["replicate"] == 2
    # And: the legacy attempt alias is preserved alongside.
    assert payload["attempt"] == 2


def test_register_run_rejects_conflicting_replicate_and_attempt(repo_root: Path) -> None:
    # Given: a study + run.
    study_id = "conflict-study"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}})
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))

    # When/Then: the alias and the new field disagree.
    with pytest.raises(ValueError, match="conflicting"):
        register_run(
            study_id=study_id,
            run_id=run_id,
            cell="A1",
            task="t",
            replicate=0,
            attempt=1,
            output_directory=runs_pool,
        )


def test_register_run_rejects_unknown_cell(repo_root: Path) -> None:
    # Given: a study declaring only A1.
    study_id = "unknown-cell-study"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}})
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))

    # When/Then
    with pytest.raises(ValueError, match=r"not declared in study\.cells"):
        register_run(
            study_id=study_id,
            run_id=run_id,
            cell="ZZ",
            task="t",
            replicate=0,
            output_directory=runs_pool,
        )


def test_register_run_requires_existing_run_manifest(repo_root: Path) -> None:
    # Given: a study with a declared cell but NO run on disk.
    study_id = "missing-run-study"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}})
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)

    # When/Then
    with pytest.raises(FileNotFoundError, match="run manifest not found"):
        register_run(
            study_id=study_id,
            run_id=uuid4(),
            cell="A1",
            task="t",
            replicate=0,
            output_directory=runs_pool,
        )


def test_register_run_enforces_task_in_cell_scope(repo_root: Path) -> None:
    # Given: a study that declares a dataset.yaml with a per-cell CVE subset.
    study_id = "scoped-study"
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True)
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "schema_version": 2,
                "cells": {"A1": {"config": "x", "harness": "ours"}},
                "dataset": "dataset.yaml",
            },
            sort_keys=False,
        )
    )
    (study_dir / "dataset.yaml").write_text(
        yaml.safe_dump(
            {
                "default_cves": ["cve-a", "cve-b"],
                "per_cell_overrides": {"A1": {"subset": ["cve-a"]}},
            },
            sort_keys=False,
        )
    )
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))

    # When/Then: registering cve-b against A1 is rejected.
    with pytest.raises(ValueError, match="not in the effective CVE set"):
        register_run(
            study_id=study_id,
            run_id=run_id,
            cell="A1",
            task="cve-b",
            replicate=0,
            output_directory=runs_pool,
        )
