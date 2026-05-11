"""Regression tests for `build_enrollment_lock` projection_status handling.

Audit N-2: rows stamped ``projection_status: "failed"`` by the harness must
be excluded from the enrollment lockfile so a silently-missing events.jsonl
no longer masquerades as a clean zero in `summary.csv`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

import yaml

from experiments.shared.scripts.collect import build_enrollment_lock


logger = logging.getLogger(__name__)


def _write_study_manifest(repo_root: Path, study_id: str) -> None:
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump({"study_id": study_id, "cells": {"A1": {}}}, sort_keys=False),
        encoding="utf-8",
    )


def _seed_run_manifest(
    runs_pool: Path,
    *,
    study_id: str,
    projection_status: str,
) -> str:
    run_id = str(uuid4())
    run_dir = runs_pool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "study_id": study_id,
                "cell": "A1",
                "task": "t",
                "replicate": 0,
                "kind": "ours",
                "projection_status": projection_status,
            }
        ),
        encoding="utf-8",
    )
    return run_id


def test_build_enrollment_lock_excludes_projection_failed_rows(repo_root: Path) -> None:
    # Given: two runs in the pool, one with projection_status=ok, one failed.
    study_id = "2026-test-projection-status"
    _write_study_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    ok_run_id = _seed_run_manifest(runs_pool, study_id=study_id, projection_status="ok")
    _seed_run_manifest(runs_pool, study_id=study_id, projection_status="failed")

    # When: the enrollment lockfile is built
    lock = build_enrollment_lock(study_id, pool_roots=[runs_pool])

    # Then: only the ok run is enrolled; the failed-projection row is dropped.
    enrolled_run_ids = {entry["run_id"] for entry in lock["enrollment"]}
    assert enrolled_run_ids == {ok_run_id}
