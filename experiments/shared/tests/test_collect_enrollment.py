"""Tests for `experiments.shared.scripts.collect.build_enrollment_lock`.

The lockfile is the only source of truth for enrollment after PR 1 of
the rearchitecture, so the walker must:

* skip runs from other studies,
* sort deterministically (cell, task, replicate, run_id),
* see runs in BOTH ``runs/`` and ``settings.output.directory``,
* refuse to silently merge two run_manifest.json files claiming the
  same run_id with conflicting fields,
* tolerate the legacy ``attempt`` field by treating it as ``replicate``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import yaml

from experiments.shared.scripts.collect import (
    DuplicateRunIdError,
    build_enrollment_lock,
    collect_study,
)


if TYPE_CHECKING:
    from pathlib import Path


def _seed_design_manifest(repo_root: Path, study_id: str) -> None:
    study_dir = repo_root / "experiments" / study_id
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "study_id": study_id,
                "schema_version": 2,
                "cells": {
                    "A1": {"config": "configs/A1.yaml", "harness": "baseline"},
                    "B2": {"config": "configs/B2.yaml", "harness": "ours"},
                },
            },
            sort_keys=False,
        )
    )


def _seed_run_manifest(
    pool: Path,
    run_id: str,
    *,
    study_id: str,
    cell: str,
    task: str,
    replicate: int | None = None,
    attempt: int | None = None,
) -> Path:
    run_dir = pool / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "run_id": run_id,
        "study_id": study_id,
        "cell": cell,
        "task": task,
        "kind": "ours",
        "exit_status": "success",
    }
    if replicate is not None:
        payload["replicate"] = replicate
    if attempt is not None:
        payload["attempt"] = attempt
    path = run_dir / "run_manifest.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def test_lockfile_excludes_other_studies(repo_root: Path) -> None:
    # Given: two runs in the same pool, one for our study and one for another.
    study_id = "study-of-interest"
    _seed_design_manifest(repo_root, study_id)
    pool = repo_root / "runs"
    _seed_run_manifest(
        pool,
        "11111111-1111-1111-1111-111111111111",
        study_id=study_id,
        cell="A1",
        task="cve-a",
        replicate=0,
    )
    _seed_run_manifest(
        pool,
        "22222222-2222-2222-2222-222222222222",
        study_id="OTHER-STUDY",
        cell="A1",
        task="cve-a",
        replicate=0,
    )

    # When
    lock = build_enrollment_lock(study_id, pool_roots=[pool])

    # Then: only the matching run shows up.
    enrolled = lock["enrollment"]
    assert [r["run_id"] for r in enrolled] == ["11111111-1111-1111-1111-111111111111"]


def test_lockfile_sorted_deterministically(repo_root: Path) -> None:
    # Given: a handful of runs deliberately seeded out of order.
    study_id = "sort-study"
    _seed_design_manifest(repo_root, study_id)
    pool = repo_root / "runs"
    seeded = [
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "B2", "cve-z", 1),
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "B2", "cve-z", 0),
        ("cccccccc-cccc-cccc-cccc-cccccccccccc", "A1", "cve-a", 0),
        ("dddddddd-dddd-dddd-dddd-dddddddddddd", "B2", "cve-a", 0),
    ]
    for run_id, cell, task, replicate in seeded:
        _seed_run_manifest(
            pool, run_id, study_id=study_id, cell=cell, task=task, replicate=replicate
        )

    # When
    lock = build_enrollment_lock(study_id, pool_roots=[pool])

    # Then: sort key is (cell, task, replicate, run_id).
    keys = [(r["cell"], r["task"], r["replicate"]) for r in lock["enrollment"]]
    assert keys == [
        ("A1", "cve-a", 0),
        ("B2", "cve-a", 0),
        ("B2", "cve-z", 0),
        ("B2", "cve-z", 1),
    ]


def test_pool_walker_finds_runs_in_both_runs_and_output(repo_root: Path) -> None:
    # Given: one run in `runs/` and another in `output/` (mirrors
    # settings.output.directory in production).
    study_id = "two-pool-study"
    _seed_design_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    output_pool = repo_root / "output"
    _seed_run_manifest(
        runs_pool,
        "11111111-1111-1111-1111-111111111111",
        study_id=study_id,
        cell="A1",
        task="cve-a",
        replicate=0,
    )
    _seed_run_manifest(
        output_pool,
        "22222222-2222-2222-2222-222222222222",
        study_id=study_id,
        cell="B2",
        task="cve-a",
        replicate=0,
    )

    # When
    lock = build_enrollment_lock(study_id, pool_roots=[runs_pool, output_pool])

    # Then: both pools were scanned.
    assert {r["run_id"] for r in lock["enrollment"]} == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }


def test_duplicate_run_id_across_roots_raises(repo_root: Path) -> None:
    # Given: the same run_id appears in two pool roots with conflicting fields.
    study_id = "dup-study"
    _seed_design_manifest(repo_root, study_id)
    runs_pool = repo_root / "runs"
    output_pool = repo_root / "output"
    run_id = "deadbeef-dead-beef-dead-beefdeadbeef"
    _seed_run_manifest(
        runs_pool,
        run_id,
        study_id=study_id,
        cell="A1",
        task="cve-a",
        replicate=0,
    )
    _seed_run_manifest(
        output_pool,
        run_id,
        study_id=study_id,
        cell="B2",
        task="cve-a",
        replicate=0,
    )

    # When/Then
    with pytest.raises(DuplicateRunIdError, match=run_id):
        build_enrollment_lock(study_id, pool_roots=[runs_pool, output_pool])


def test_legacy_attempt_field_resolves_to_replicate(repo_root: Path) -> None:
    # Given: a run manifest that pre-dates the rename (only `attempt`).
    study_id = "legacy-field-study"
    _seed_design_manifest(repo_root, study_id)
    pool = repo_root / "runs"
    _seed_run_manifest(
        pool,
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        study_id=study_id,
        cell="A1",
        task="cve-a",
        attempt=7,
    )

    # When
    lock = build_enrollment_lock(study_id, pool_roots=[pool])

    # Then: the lockfile carries the new field name.
    assert lock["enrollment"][0]["replicate"] == 7


def test_collect_study_writes_lockfile_and_sidecar(repo_root: Path) -> None:
    # Given: a seeded study + run.
    study_id = "write-test-study"
    _seed_design_manifest(repo_root, study_id)
    pool = repo_root / "runs"
    _seed_run_manifest(
        pool,
        "11111111-1111-1111-1111-111111111111",
        study_id=study_id,
        cell="A1",
        task="cve-a",
        replicate=0,
    )
    reports_dir = repo_root / "experiments" / study_id / "reports"
    reports_dir.mkdir(parents=True)

    # When
    output = collect_study(study_id, pool_roots=[pool])

    # Then: the lockfile is on disk, sidecar entry exists.
    assert output.is_file()
    assert output.name == "enrollment.lock.yaml"
    sidecar = reports_dir / ".generated.json"
    assert sidecar.is_file()
    sidecar_data = json.loads(sidecar.read_text())
    rel_key = f"experiments/{study_id}/reports/enrollment.lock.yaml"
    assert rel_key in sidecar_data
    assert sidecar_data[rel_key]["generated_by"].endswith("collect.py")

    # And: the lockfile body is parseable YAML carrying the enrollment list.
    parsed = yaml.safe_load(output.read_text())
    assert parsed["study_id"] == study_id
    assert len(parsed["enrollment"]) == 1
