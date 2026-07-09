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


def test_write_run_manifest_uses_unique_tmp_filename(repo_root: Path) -> None:
    # Given: a finished run manifest. Two consecutive _write_run_manifest
    # calls should never reuse the same staging path; pre-fix they used
    # the deterministic `<name>.tmp` which races under N-4 + N-11.
    from experiments.shared.scripts.register_run import _write_run_manifest

    study_id = "tmp-name-study"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x"}})
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))
    manifest_path = runs_pool / str(run_id) / "run_manifest.json"

    captured: list[str] = []
    import tempfile as _tempfile

    real_mkstemp = _tempfile.mkstemp

    def _capture(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        captured.append(name)
        return fd, name

    import experiments.shared.scripts.register_run as register_module

    original = register_module.tempfile.mkstemp
    register_module.tempfile.mkstemp = _capture
    try:
        _write_run_manifest(manifest_path, {"run_id": str(run_id), "v": 1})
        _write_run_manifest(manifest_path, {"run_id": str(run_id), "v": 2})
    finally:
        register_module.tempfile.mkstemp = original

    # Then: two distinct tmp paths were used.
    assert len(captured) == 2
    assert captured[0] != captured[1]
    # And: the final manifest reflects the last write.
    assert json.loads(manifest_path.read_text())["v"] == 2


def test_write_run_manifest_fsyncs_before_replace(repo_root: Path) -> None:
    # Given: a finished run manifest. Atomic-write helpers in this repo
    # uniformly flush + fsync before the rename so a power loss between the
    # write and durable storage cannot leave the old manifest visible after
    # rename returned. register_run used mkstemp + replace without fsync
    # pre-fix; this test pins the fsync into the contract.
    from experiments.shared.scripts.register_run import _write_run_manifest

    study_id = "fsync-study"
    _write_study_manifest(repo_root, study_id, cells={"A1": {"config": "x"}})
    runs_pool = repo_root / "runs"
    runs_pool.mkdir(parents=True, exist_ok=True)
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))
    manifest_path = runs_pool / str(run_id) / "run_manifest.json"

    fsync_calls: list[int] = []
    import os as _os

    real_fsync = _os.fsync

    def _spy(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    import experiments.shared.scripts.register_run as register_module

    register_module.os.fsync = _spy
    try:
        _write_run_manifest(manifest_path, {"run_id": str(run_id), "v": 1})
    finally:
        register_module.os.fsync = real_fsync

    # Then: fsync was invoked at least once during the staging write.
    assert fsync_calls, "expected fsync before tmp->target replace"


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
