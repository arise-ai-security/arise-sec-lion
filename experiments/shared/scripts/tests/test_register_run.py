"""Tests for `experiments.shared.scripts.register_run`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import yaml

from experiments.shared.scripts import register_run as register_run_module
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
                "created_at": "2026-04-22T00:00:00Z",
                "cells": cells,
                "runs": [],
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


def test_register_run_merges_both_manifests(repo_root: Path) -> None:
    # Given: a study manifest declaring cell B2, and a finished run in the pool.
    study_id = "2026-04-22-naive-vs-cybersec"
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
        attempt=0,
        output_directory=runs_pool,
    )

    # Then: the run manifest now carries experiment-level fields.
    updated = json.loads(run_manifest.read_text())
    assert updated["study_id"] == study_id
    assert updated["cell"] == "B2"
    assert updated["task"] == "gpac.cve-2021-40575"
    assert updated["attempt"] == 0

    # And: the study manifest lists the run exactly once.
    study_manifest = yaml.safe_load(
        (repo_root / "experiments" / study_id / "manifest.yaml").read_text()
    )
    rows = study_manifest["runs"]
    assert len(rows) == 1
    assert rows[0] == {
        "run_id": str(run_id),
        "cell": "B2",
        "task": "gpac.cve-2021-40575",
        "attempt": 0,
    }


def test_register_run_is_idempotent(repo_root: Path) -> None:
    # Given: a valid study + run.
    study_id = "idempotency-study"
    _write_study_manifest(
        repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}}
    )
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))

    # When: registered twice.
    kwargs = {
        "study_id": study_id,
        "run_id": run_id,
        "cell": "A1",
        "task": "t",
        "attempt": 0,
        "output_directory": runs_pool,
    }
    register_run(**kwargs)
    register_run(**kwargs)

    # Then: still only one row in the study manifest.
    study = yaml.safe_load(
        (repo_root / "experiments" / study_id / "manifest.yaml").read_text()
    )
    assert len(study["runs"]) == 1


def test_register_run_rejects_unknown_cell(repo_root: Path) -> None:
    # Given: a study declaring only A1.
    study_id = "unknown-cell-study"
    _write_study_manifest(
        repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}}
    )
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
            attempt=0,
            output_directory=runs_pool,
        )


def test_register_run_requires_existing_run_manifest(repo_root: Path) -> None:
    # Given: a study with a declared cell but NO run on disk.
    study_id = "missing-run-study"
    _write_study_manifest(
        repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}}
    )
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)

    # When/Then
    with pytest.raises(FileNotFoundError, match="run manifest not found"):
        register_run(
            study_id=study_id,
            run_id=uuid4(),
            cell="A1",
            task="t",
            attempt=0,
            output_directory=runs_pool,
        )


def test_register_run_survives_run_manifest_write_failure(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transactional ordering (must-fix #4): study manifest is written FIRST.

    If the run_manifest write fails after the study manifest succeeded, the
    study is durably enrolled and the operator can recover by re-running
    register_run (which is idempotent). The failure must surface, not be
    silently swallowed.
    """
    # Given: a valid study + seeded run_manifest.
    study_id = "txn-study"
    _write_study_manifest(
        repo_root, study_id, cells={"A1": {"config": "x", "harness": "ours"}}
    )
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    run_manifest = _seed_run_manifest(runs_pool, str(run_id))
    original_run_manifest_bytes = run_manifest.read_bytes()

    # Fail the run_manifest write ONCE, then restore the real writer so the
    # recovery re-run backfills it cleanly.
    real_write = register_run_module._write_run_manifest
    calls = {"count": 0}

    def _one_shot_boom(path, payload):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("disk full during run_manifest write")
        return real_write(path, payload)

    monkeypatch.setattr(register_run_module, "_write_run_manifest", _one_shot_boom)

    kwargs = {
        "study_id": study_id,
        "run_id": run_id,
        "cell": "A1",
        "task": "t",
        "attempt": 0,
        "output_directory": runs_pool,
    }

    # When/Then: the first call fails.
    with pytest.raises(OSError, match="disk full"):
        register_run(**kwargs)

    # Then: the study manifest is durably enrolled (write-first ordering).
    study = yaml.safe_load(
        (repo_root / "experiments" / study_id / "manifest.yaml").read_text()
    )
    assert len(study["runs"]) == 1
    assert study["runs"][0]["run_id"] == str(run_id)
    # And: the run_manifest is untouched — recoverable via re-run.
    assert run_manifest.read_bytes() == original_run_manifest_bytes

    # When: the operator re-runs register_run (idempotent backfill).
    register_run(**kwargs)

    # Then: run_manifest now carries the experiment fields, study still has one row.
    updated = json.loads(run_manifest.read_text())
    assert updated["study_id"] == study_id
    assert updated["cell"] == "A1"
    study_after = yaml.safe_load(
        (repo_root / "experiments" / study_id / "manifest.yaml").read_text()
    )
    assert len(study_after["runs"]) == 1


def test_register_run_enforces_task_in_cell_scope(repo_root: Path) -> None:
    # Given: a study that declares a dataset.yaml with a per-cell CVE subset.
    study_id = "scoped-study"
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True)
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "cells": {"A1": {"config": "x", "harness": "ours"}},
                "dataset": "dataset.yaml",
                "runs": [],
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
            attempt=0,
            output_directory=runs_pool,
        )
