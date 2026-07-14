"""Tests for the idempotent N1 SEC-bench full-run commands."""

from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
N1_DIR = REPO_ROOT / "experiments" / "n1-secbench-full"
sys.path.insert(0, str(N1_DIR))

import _n1_experiment as n1  # noqa: E402
import export_data  # noqa: E402
import prepare_n1_experiment as prepare  # noqa: E402
import run_batch  # noqa: E402


def _definition(tasks: tuple[str, ...]) -> n1.StudyDefinition:
    return n1.StudyDefinition(
        tasks=tasks,
        fixtures={},
        base_images={},
        config_path=Path("config.yaml"),
        manifest_path=Path("manifest.yaml"),
        dataset_path=Path("dataset.yaml"),
    )


def _record(task: str, run_id: str, run_dir: Path, status: str = "success") -> n1.RunRecord:
    return n1.RunRecord(
        run_id=run_id,
        task=task,
        exit_status=status,
        replicate=0,
        run_dir=run_dir,
    )


def test_committed_definition_is_the_full_eval_split() -> None:
    # Given: the committed N1 experiment definition.
    definition = n1.load_definition()

    # When: its fixed scope is inspected.
    tasks = definition.tasks

    # Then: it is exactly one complete, unique 300-task SEC-bench eval run.
    assert len(tasks) == n1.EXPECTED_TASK_COUNT
    assert len(set(tasks)) == len(tasks)
    assert set(definition.fixtures) == set(tasks)
    assert set(definition.base_images) == set(tasks)


def test_sharding_and_resume_are_deterministic(tmp_path: Path) -> None:
    # Given: ten tasks, three host shards, and one completed task.
    tasks = tuple(f"task-{index}" for index in range(10))
    shard = n1.parse_shard("2/3")
    run_dir = tmp_path / "run-4"
    run_dir.mkdir()
    records = [_record("task-4", "run-4", run_dir)]

    # When: the same shard is planned twice around the completed manifest.
    selected = n1.tasks_for_shard(tasks, shard)
    first_plan = n1.pending_tasks(selected, records)
    second_plan = n1.pending_tasks(selected, records)

    # Then: selection is stable and completed work is never planned again.
    assert selected == ["task-1", "task-4", "task-7"]
    assert first_plan == second_plan == ["task-1", "task-7"]


def test_run_defaults_bound_disk_and_enable_concurrency() -> None:
    # Given: the operator supplies no performance overrides.
    args = run_batch._parser().parse_args([])

    # When/Then: two tasks run concurrently and images are evicted by default.
    assert args.batch_size == 30
    assert args.parallel == 2
    assert args.shard.label == "1/1"
    assert args.keep_images is False


def test_duplicate_task_manifests_fail_closed(tmp_path: Path) -> None:
    # Given: two run manifests claiming the same N1 task and replicate.
    runs_root = tmp_path / "runs"
    run_ids = (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    for run_id in run_ids:
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        manifest = {
            "cell": n1.CELL_ID,
            "exit_status": "success",
            "replicate": 0,
            "run_id": run_id,
            "study_id": n1.STUDY_ID,
            "task": "task-a",
        }
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # When/Then: resume refuses ambiguous state instead of choosing a run.
    with pytest.raises(n1.ExperimentStateError, match="duplicate runs"):
        n1.load_run_records(_definition(("task-a",)), repo_root=tmp_path)


def test_prepare_checks_full_toolchain_and_database_contract() -> None:
    # Given: the prepare command's prerequisite and event-store contracts.
    required_tools = set(prepare.REQUIRED_PROGRAMS)
    valid_status = (
        "12|t|t|event_id,aggregate_id,sequence_number,event_type,payload,"
        "occurred_at,metadata|t"
    )

    # When/Then: all run/export tools are checked and a valid schema reports its event count.
    assert {"docker", "uv", "psql", "pg_dump", "xargs"} <= required_tools
    assert prepare._parse_database_status(valid_status) == 12

    # And: a database without the OCC uniqueness invariant is rejected.
    invalid_status = valid_status[:-1] + "f"
    with pytest.raises(n1.ExperimentError, match="UNIQUE"):
        prepare._parse_database_status(invalid_status)


def test_run_archive_replaces_previous_export(tmp_path: Path) -> None:
    # Given: one completed run and an export target.
    run_dir = tmp_path / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    (run_dir / "artifact.txt").write_text("result", encoding="utf-8")
    records = [_record("task-a", "run-a", run_dir)]
    target = tmp_path / "runs.tar.gz"

    # When: export is repeated.
    export_data._write_runs_archive(records, target)
    export_data._write_runs_archive(records, target)

    # Then: the stable archive contains one run, not two appended copies.
    with tarfile.open(target, mode="r:gz") as archive:
        names = archive.getnames()
    assert names.count("runs/run-a/artifact.txt") == 1


def test_n1_commands_do_not_load_persisted_secrets() -> None:
    # Given: the three operator-facing N1 commands.
    paths = [
        N1_DIR / "prepare_n1_experiment.py",
        N1_DIR / "run_batch.py",
        N1_DIR / "export_data.py",
    ]

    # When: their credential behavior is inspected.
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()

    # Then: they accept process environment values and never invoke a secret store.
    assert "bitwarden" not in combined
    assert "bw " not in combined
    assert "source deployment/.env" not in combined
