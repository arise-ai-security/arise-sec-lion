"""Direct tests for `validate_reports.validate_study`.

PR 1 added the study-level safety net: lockfile present, manifest present,
manifest carries no `runs:` key, and every enrolled `run_id` resolves to a
real `run_manifest.json` in the pool. The walker in
``test_validate_reports.py`` covers per-file provenance; this module
exercises each branch of the new helper directly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import yaml

from experiments.shared.scripts.collect import collect_study
from experiments.shared.scripts.validate_reports import validate_study


if TYPE_CHECKING:
    from pathlib import Path


def _write_manifest(repo_root: Path, study_id: str, *, extra: dict | None = None) -> Path:
    """Seed `experiments/<study>/manifest.yaml` (design-only by default)."""
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = study_dir / "manifest.yaml"
    payload: dict = {
        "study_id": study_id,
        "schema_version": 2,
        "cells": {"A1": {"config": "x", "harness": "ours"}},
    }
    if extra:
        payload.update(extra)
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return manifest_path


def _seed_run(runs_pool: Path, run_id: str, *, study_id: str, cell: str, task: str) -> Path:
    """Write a `run_manifest.json` carrying the experiment-level fields."""
    run_dir = runs_pool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "ours",
                "exit_status": "success",
                "study_id": study_id,
                "cell": cell,
                "task": task,
                "replicate": 0,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return manifest


def test_validate_study_happy_path(repo_root: Path) -> None:
    # Given: a study with a design-only manifest, a fully-enrolled run in the
    # pool, and a freshly-rendered lockfile that points at it.
    study_id = "happy-study"
    _write_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    _seed_run(runs_pool, "run-aaaa", study_id=study_id, cell="A1", task="t")
    collect_study(study_id, pool_roots=[runs_pool])

    # When
    errors = validate_study(study_id)

    # Then: no errors — every invariant holds.
    assert errors == []


def test_validate_study_missing_lockfile(repo_root: Path) -> None:
    # Given: a manifest is present but the lockfile was never rendered.
    study_id = "no-lock-study"
    _write_manifest(repo_root, study_id)

    # When
    errors = validate_study(study_id)

    # Then: a single error pointing at enrollment.lock.yaml that hints at the
    # remediation command.
    assert len(errors) == 1
    assert "enrollment.lock.yaml" in errors[0]
    assert "experiments.shared.scripts.collect" in errors[0]


def test_validate_study_manifest_has_runs_key(repo_root: Path) -> None:
    # Given: a regression where someone reintroduced a `runs:` key into the
    # design file. The lockfile is present and consistent with the pool.
    study_id = "regression-study"
    _write_manifest(repo_root, study_id, extra={"runs": [{"run_id": "x"}]})
    runs_pool = repo_root / "runs"
    _seed_run(runs_pool, "run-aaaa", study_id=study_id, cell="A1", task="t")
    collect_study(study_id, pool_roots=[runs_pool])

    # When
    errors = validate_study(study_id)

    # Then: the regression is flagged.
    assert any("contains a `runs:` key" in e for e in errors), errors


def test_validate_study_detects_cell_drift_between_lock_and_manifest(repo_root: Path) -> None:
    # Given: a study with the lockfile enrolling run-aaaa @ (A1, t, 0), but
    # someone mutates the run's manifest in place to claim a different cell
    # WITHOUT re-running collect.
    study_id = "cell-drift-study"
    _write_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    manifest = _seed_run(runs_pool, "run-aaaa", study_id=study_id, cell="A1", task="t")
    collect_study(study_id, pool_roots=[runs_pool])

    # Drift the manifest after the lockfile captured (A1, t, 0).
    record = json.loads(manifest.read_text())
    record["cell"] = "B2"  # claims a different cell now
    manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    # When
    errors = validate_study(study_id)

    # Then: drift is flagged with the offending tuple inline.
    assert any("enrollment tuple drift" in e for e in errors), errors


def test_validate_study_detects_replicate_drift(repo_root: Path) -> None:
    # Given: the same setup with replicate mutated instead of cell.
    study_id = "replicate-drift-study"
    _write_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    manifest = _seed_run(runs_pool, "run-cccc", study_id=study_id, cell="A1", task="t")
    collect_study(study_id, pool_roots=[runs_pool])

    record = json.loads(manifest.read_text())
    record["replicate"] = 99
    manifest.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    # When
    errors = validate_study(study_id)

    # Then: drift is flagged.
    assert any("enrollment tuple drift" in e for e in errors), errors


def test_validate_study_unresolved_run_id(repo_root: Path) -> None:
    # Given: a lockfile listing a run_id that no longer has a run_manifest.json
    # in any pool root (the run dir was deleted or never made it across).
    study_id = "missing-run-study"
    _write_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    _seed_run(runs_pool, "run-bbbb", study_id=study_id, cell="A1", task="t")
    collect_study(study_id, pool_roots=[runs_pool])

    # And: drop the run from the pool after the lockfile captured it.
    import shutil

    shutil.rmtree(runs_pool / "run-bbbb")

    # When
    errors = validate_study(study_id)

    # Then: the unresolved run_id is named in the error.
    assert any("run-bbbb" in e and "no run_manifest.json" in e for e in errors), errors
