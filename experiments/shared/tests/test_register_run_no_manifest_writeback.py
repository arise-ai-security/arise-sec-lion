"""Regression tests: register_run does not mutate the study manifest.

PR 1 of the experiments rearchitecture moved enrollment out of
``manifest.yaml`` and into a derived ``reports/enrollment.lock.yaml``.
This module asserts the new boundary explicitly so a future refactor
can't quietly reintroduce manifest writebacks.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import yaml

from experiments.shared.scripts.register_run import register_run


if TYPE_CHECKING:
    from pathlib import Path


def _write_design_only_manifest(repo_root: Path, study_id: str) -> tuple[bytes, Path]:
    """Seed a design-only manifest (no `runs:` key) and return its bytes."""
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True)
    manifest_path = study_dir / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "schema_version": 2,
                "cells": {"A1": {"config": "x", "harness": "ours"}},
            },
            sort_keys=False,
        )
    )
    return manifest_path.read_bytes(), manifest_path


def _seed_run_manifest(runs_pool: Path, run_id: str) -> Path:
    run_dir = runs_pool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "kind": "ours",
        "exit_status": "success",
    }
    path = run_dir / "run_manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def test_register_run_does_not_modify_study_manifest(repo_root: Path) -> None:
    # Given: a design-only manifest and a finished run.
    study_id = "no-writeback-study"
    original_bytes, manifest_path = _write_design_only_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    _seed_run_manifest(runs_pool, str(run_id))

    # When: register_run completes successfully.
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell="A1",
        task="t",
        replicate=0,
        output_directory=runs_pool,
    )

    # Then: the manifest is byte-identical to before — no `runs:` key sneaks in.
    assert manifest_path.read_bytes() == original_bytes
    parsed = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert "runs" not in parsed


def test_register_run_still_stamps_per_run_manifest(repo_root: Path) -> None:
    # Given: a design-only manifest and a finished run.
    study_id = "stamping-study"
    _write_design_only_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    run_id = uuid4()
    run_manifest = _seed_run_manifest(runs_pool, str(run_id))

    # When
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell="A1",
        task="cve-x",
        replicate=3,
        output_directory=runs_pool,
    )

    # Then: the per-run manifest carries the experiment fields including
    # both the new replicate and the legacy attempt alias.
    payload = json.loads(run_manifest.read_text())
    assert payload["study_id"] == study_id
    assert payload["cell"] == "A1"
    assert payload["task"] == "cve-x"
    assert payload["replicate"] == 3
    assert payload["attempt"] == 3
