"""Tests for the idempotent N1 SEC-bench full-run commands."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest


if TYPE_CHECKING:
    from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[2]
N1_DIR = REPO_ROOT / "experiments" / "n1-secbench-full"
sys.path.insert(0, str(N1_DIR))

import _n1_experiment as n1  # noqa: E402
import export_data  # noqa: E402
import prepare_n1_experiment as prepare  # noqa: E402
import run_batch  # noqa: E402


def _load_script_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _diagnostic_event(
    run_id: str,
    sequence_number: int,
    event_type: str,
    **payload: object,
) -> run_batch.FailureDiagnosticEvent:
    return run_batch.FailureDiagnosticEvent(
        event_id=f"00000000-0000-0000-0000-{sequence_number:012d}",
        aggregate_id=run_id,
        sequence_number=sequence_number,
        event_type=event_type,
        occurred_at=f"2026-07-25T00:00:{sequence_number:02d}+00:00",
        payload=payload,
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


def test_n1_control_config_rejects_host_procedural_dispatch() -> None:
    # Given: an N1-like flat OpenHands configuration with Host procedures enabled.
    config = {
        "orchestration": {
            "mode": "flat",
            "procedural_dispatch": True,
            "skip_judge": True,
        },
        "worker": {
            "tool": "openhands",
            "tool_params": {"openhands": {"enable_subagents": False}},
        },
        "output": {"directory": "./runs"},
    }

    # When + Then: the study-specific loader contract fails closed.
    with pytest.raises(n1.ExperimentDefinitionError, match="procedural dispatch"):
        n1._validate_n1_control_config(config)


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


def test_postgres_clients_run_inside_the_single_database_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Docker is available on the host.
    monkeypatch.setattr(n1, "require_program", lambda name: f"/usr/bin/{name}")

    # When: a PostgreSQL client command is assembled.
    argv = n1.postgres_exec_argv("psql", "--version")

    # Then: the client runs in postgres-main, not from a Homebrew installation.
    assert argv == ["/usr/bin/docker", "exec", "postgres-main", "psql", "--version"]


def test_failure_diagnostic_query_is_scoped_to_the_persisted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one failed canonical run and a recording PostgreSQL subprocess.
    run_id = "00000000-0000-0000-0000-000000000104"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    record = _record("task-a", run_id, run_dir, status="failed")
    prompt_payload = json.dumps(
        {
            "event_id": "00000000-0000-0000-0000-000000000106",
            "aggregate_id": run_id,
            "sequence_number": 8,
            "event_type": "PromptSent",
            "occurred_at": "2026-07-25T00:00:08+00:00",
            "payload": {},
        }
    )
    failure_payload = json.dumps(
        {
            "event_id": "00000000-0000-0000-0000-000000000105",
            "aggregate_id": run_id,
            "sequence_number": 9,
            "event_type": "WorkFailed",
            "occurred_at": "2026-07-25T00:00:09+00:00",
            "payload": {"reason": "provider authentication rejected"},
        }
    )
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=prompt_payload + "\n" + failure_payload + "\n",
            stderr="",
        )

    monkeypatch.setattr(
        run_batch,
        "postgres_exec_argv",
        lambda program, *args: ["/usr/bin/docker", "exec", program, *args],
    )
    monkeypatch.setattr(run_batch.subprocess, "run", fake_run)

    # When: diagnostics are loaded from the authoritative event store.
    events = run_batch._load_failure_diagnostic_events(record)

    # Then: the SQL is scoped by the validated UUID and the result is typed.
    assert run_id in calls[0][-1]
    assert "RUN_ID_PLACEHOLDER" not in calls[0][-1]
    assert events == [
        run_batch.FailureDiagnosticEvent(
            event_id="00000000-0000-0000-0000-000000000105",
            aggregate_id=run_id,
            sequence_number=9,
            event_type="WorkFailed",
            occurred_at="2026-07-25T00:00:09+00:00",
            payload={"reason": "provider authentication rejected"},
        )
    ]


def test_run_defaults_bound_disk_and_enable_concurrency() -> None:
    # Given: the operator supplies no performance overrides.
    args = run_batch._parser().parse_args([])

    # When/Then: two tasks run concurrently and images are evicted by default.
    assert args.batch_size == 30
    assert args.parallel == 2
    assert args.shard.label == "1/1"
    assert args.instances is None
    assert args.keep_images is False
    assert args.force is False


def test_wave_eviction_keeps_the_shared_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: one completed wave and a recording Docker subprocess.
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(run_batch.subprocess, "run", fake_run)

    # When: N1 reclaims the wave's image storage.
    run_batch._evict_wave(
        "/usr/bin/docker",
        ["task-a"],
        {"task-a": "hwiwonlee/secb.eval.x86_64.task-a:patch"},
    )

    # Then: only the final CVE image, its upstream base, and build cache are reclaimed.
    assert calls == [
        [
            "/usr/bin/docker",
            "image",
            "rm",
            "secb-tools:task-a-patch",
            "hwiwonlee/secb.eval.x86_64.task-a:patch",
        ],
        ["/usr/bin/docker", "builder", "prune", "--force"],
    ]
    assert all("payload-focal-amd64" not in argument for call in calls for argument in call)


def test_explicit_instances_limit_the_batch_scope() -> None:
    # Given: the operator requests the shared two-instance validation pair.
    tasks = (
        "other.instance",
        "openexr.cve-2020-16589",
        "faad2.cve-2018-20196",
    )
    instances = run_batch._instances("openexr.cve-2020-16589,faad2.cve-2018-20196")

    # When: the batch scope is selected.
    selected = run_batch._selected_tasks(
        tasks,
        shard=n1.parse_shard("1/1"),
        instances=instances,
    )

    # Then: only those two instances are eligible to build or launch.
    assert selected == [
        "openexr.cve-2020-16589",
        "faad2.cve-2018-20196",
    ]


def test_explicit_instances_reject_unknown_or_sharded_scope() -> None:
    tasks = ("known.instance",)

    with pytest.raises(n1.ExperimentError, match="unknown SEC-bench"):
        run_batch._selected_tasks(
            tasks,
            shard=n1.parse_shard("1/1"),
            instances=("unknown.instance",),
        )

    with pytest.raises(n1.ExperimentError, match="cannot be combined"):
        run_batch._selected_tasks(
            tasks,
            shard=n1.parse_shard("1/2"),
            instances=("known.instance",),
        )


def test_completed_explicit_scope_does_not_require_provider_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the requested instance already has an authoritative run record.
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    definition = _definition(("task-a",))
    record = _record("task-a", "run-a", run_dir)
    monkeypatch.setattr(run_batch, "load_definition", lambda: definition)
    monkeypatch.setattr(run_batch, "load_run_records", lambda *_args, **_kwargs: [record])
    monkeypatch.setattr(
        run_batch,
        "_require_runtime",
        lambda: pytest.fail("completed resume must not require provider runtime"),
    )
    args = run_batch._parser().parse_args(["--instances", "task-a"])

    # When/Then: resume completes without an OpenAI key, Docker, or a new launch.
    run_batch._execute(args)


def test_force_requires_an_explicit_instance_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a force request without --instances or the wrapper's --smoke expansion.
    definition = _definition(("task-a",))
    monkeypatch.setattr(run_batch, "load_definition", lambda: definition)
    monkeypatch.setattr(run_batch, "load_run_records", lambda *_args, **_kwargs: [])
    args = run_batch._parser().parse_args(["--force"])

    # When + Then: the runner rejects an accidental full-dataset rerun.
    with pytest.raises(n1.ExperimentError, match="requires --instances"):
        run_batch._execute(args)


def test_force_runs_completed_instance_and_reports_only_the_new_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: a canonical failed run already exists for the requested task.
    old_run_dir = tmp_path / "00000000-0000-0000-0000-000000000201"
    new_run_dir = tmp_path / "00000000-0000-0000-0000-000000000202"
    old_run_dir.mkdir()
    new_run_dir.mkdir()
    old_record = _record(
        "task-a",
        "00000000-0000-0000-0000-000000000201",
        old_run_dir,
        status="failed",
    )
    new_record = _record(
        "task-a",
        "00000000-0000-0000-0000-000000000202",
        new_run_dir,
        status="success",
    )
    definition = _definition(("task-a",))
    history_calls = 0

    def fake_history(*_args: object, **_kwargs: object) -> list[n1.RunRecord]:
        nonlocal history_calls
        history_calls += 1
        return [old_record] if history_calls == 1 else [old_record, new_record]

    launched: list[tuple[list[str], int]] = []
    monkeypatch.setattr(run_batch, "load_definition", lambda: definition)
    monkeypatch.setattr(
        run_batch,
        "load_run_records",
        lambda *_args, **_kwargs: [old_record],
    )
    monkeypatch.setattr(run_batch, "load_run_history", fake_history)
    monkeypatch.setattr(run_batch, "_require_runtime", lambda: "/usr/bin/docker")
    monkeypatch.setattr(run_batch, "_provision_wave", lambda *_args: None)
    monkeypatch.setattr(
        run_batch,
        "_run_wave",
        lambda tasks, parallel: launched.append((tasks, parallel)) or 0,
    )
    monkeypatch.setattr(run_batch, "_evict_wave", lambda *_args: None)
    monkeypatch.setattr(
        run_batch,
        "_log_failure_diagnostics",
        lambda record: pytest.fail(f"new successful run was misclassified: {record.run_id}"),
    )
    args = run_batch._parser().parse_args(["--force", "--instances", "task-a", "--parallel", "1"])

    # When: force executes the already-completed instance.
    with caplog.at_level(logging.INFO):
        run_batch._execute(args)

    # Then: the fresh success is reported without replacing the failed canonical run.
    output = caplog.text
    assert "[run] shard=1/1 tasks=1 completed=0 pending=1 parallel=1 force=true" in output
    assert "done: shard=1/1 completed=1 succeeded=1 failed=0" in output
    assert "canonical first-launch records remain unchanged" in output
    assert launched == [(["task-a"], 1)]
    assert history_calls == 2


def test_force_rejects_a_wave_without_exactly_one_new_event_backed_run(
    tmp_path: Path,
) -> None:
    # Given: the history contains only the run that existed before force.
    run_dir = tmp_path / "00000000-0000-0000-0000-000000000203"
    run_dir.mkdir()
    existing = _record(
        "task-a",
        "00000000-0000-0000-0000-000000000203",
        run_dir,
    )

    # When + Then: no new run cannot masquerade as the forced attempt.
    with pytest.raises(n1.ExperimentError, match="did not create a new"):
        run_batch._new_forced_wave_records(
            ["task-a"],
            [existing],
            {existing.run_id},
        )

    # And: multiple new runs are rejected as ambiguous.
    new_a = _record(
        "task-a",
        "00000000-0000-0000-0000-000000000204",
        tmp_path / "00000000-0000-0000-0000-000000000204",
    )
    new_b = _record(
        "task-a",
        "00000000-0000-0000-0000-000000000205",
        tmp_path / "00000000-0000-0000-0000-000000000205",
    )
    with pytest.raises(n1.ExperimentError, match="created multiple new"):
        run_batch._new_forced_wave_records(
            ["task-a"],
            [existing, new_a, new_b],
            {existing.run_id},
        )


def test_completed_failed_scope_prints_persisted_root_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: a resumed task whose canonical event-backed run already failed.
    run_id = "00000000-0000-0000-0000-000000000101"
    run_dir = tmp_path / run_id
    (run_dir / "testcase").mkdir(parents=True)
    (run_dir / "src").mkdir()
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "task": "task-a",
                "exit_status": "failed",
                "started_at": "2026-07-25T00:00:00Z",
                "ended_at": "2026-07-25T00:01:00Z",
                "summary_available": True,
                "deliverables": {"security_report.md": False},
            }
        ),
        encoding="utf-8",
    )
    record = _record("task-a", run_id, run_dir, status="failed")
    definition = _definition(("task-a",))
    secret = "sk-this-must-not-appear"  # noqa: S105 - fake redaction sentinel
    reason = (
        "OpenHands adapter failed after validating the runtime boundary; "
        f"Authorization: Bearer {secret}"
    )
    events = [
        _diagnostic_event(run_id, 1, "AgentCreated", role="boss"),
        _diagnostic_event(run_id, 2, "OperationStarted", operation_type="worker_execution"),
        _diagnostic_event(run_id, 3, "WorkFailed", reason=reason),
        _diagnostic_event(
            run_id,
            4,
            "RunCompleted",
            status="failed",
            duration_seconds=60.0,
            total_agents=1,
            completed_agents=0,
            failed_agents=1,
        ),
    ]
    monkeypatch.setattr(run_batch, "load_definition", lambda: definition)
    monkeypatch.setattr(run_batch, "load_run_records", lambda *_args, **_kwargs: [record])
    monkeypatch.setattr(run_batch, "_load_failure_diagnostic_events", lambda _record: events)
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(
        run_batch,
        "_require_runtime",
        lambda: pytest.fail("completed resume must not require provider runtime"),
    )
    args = run_batch._parser().parse_args(["--instances", "task-a"])

    # When: the failed task is selected again without rerunning it.
    with caplog.at_level(logging.INFO):
        run_batch._execute(args)

    # Then: the current output resolves the old run and its complete persisted cause.
    output = caplog.text
    assert "completed=1 pending=0" in output
    assert "done: shard=1/1 completed=1 succeeded=0 failed=1" in output
    assert f"[failure] begin task=task-a run_id={run_id} exit_status=failed" in output
    assert f"[failure] manifest={run_dir / 'run_manifest.json'}" in output
    assert "[failure] diagnostic_events=4 failure_causes=1" in output
    assert f"[failure] cause=1/1 aggregate_id={run_id} type=WorkFailed" in output
    assert "OpenHands adapter failed after validating the runtime boundary" in output
    assert "type=RunCompleted" in output
    assert secret not in output
    assert "Bearer <redacted>" in output


def test_new_failed_run_prints_the_same_persisted_root_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: one pending task that produces a failed event-backed record.
    run_id = "00000000-0000-0000-0000-000000000102"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"run_id": run_id, "task": "task-a", "exit_status": "failed"}),
        encoding="utf-8",
    )
    record = _record("task-a", run_id, run_dir, status="failed")
    definition = _definition(("task-a",))
    records_calls = 0

    def fake_records(*_args: object, **_kwargs: object) -> list[n1.RunRecord]:
        nonlocal records_calls
        records_calls += 1
        return [] if records_calls == 1 else [record]

    diagnostic_calls: list[str] = []

    def fake_diagnostics(selected: n1.RunRecord) -> list[run_batch.FailureDiagnosticEvent]:
        diagnostic_calls.append(selected.run_id)
        return [
            _diagnostic_event(
                run_id,
                1,
                "VerificationFailed",
                failed_stage="execution",
                feedback="patched binary still reproduces the sanitizer failure",
                stages_passed=["structural"],
                score=0,
            ),
            _diagnostic_event(
                run_id,
                2,
                "RunCompleted",
                status="failed",
                failed_agents=1,
            ),
        ]

    monkeypatch.setattr(run_batch, "load_definition", lambda: definition)
    monkeypatch.setattr(run_batch, "load_run_records", fake_records)
    monkeypatch.setattr(run_batch, "_require_runtime", lambda: "/usr/bin/docker")
    monkeypatch.setattr(run_batch, "_provision_wave", lambda *_args: None)
    monkeypatch.setattr(run_batch, "_run_wave", lambda *_args: 1)
    monkeypatch.setattr(run_batch, "_evict_wave", lambda *_args: None)
    monkeypatch.setattr(run_batch, "_load_failure_diagnostic_events", fake_diagnostics)
    args = run_batch._parser().parse_args(["--instances", "task-a"])

    # When: the batch executes and records the failed outcome.
    with caplog.at_level(logging.INFO):
        run_batch._execute(args)

    # Then: the newly produced failure is diagnosed from persisted state too.
    output = caplog.text
    assert "recorded one or more failed experimental outcomes" in output
    assert "done: shard=1/1 completed=1 succeeded=0 failed=1" in output
    assert "type=VerificationFailed" in output
    assert 'failed_stage="execution"' in output
    assert "patched binary still reproduces the sanitizer failure" in output
    assert diagnostic_calls == [run_id]


def test_failed_scope_rejects_missing_persisted_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a failed manifest whose event history has only a terminal status.
    run_id = "00000000-0000-0000-0000-000000000103"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"run_id": run_id, "task": "task-a", "exit_status": "failed"}),
        encoding="utf-8",
    )
    record = _record("task-a", run_id, run_dir, status="failed")
    monkeypatch.setattr(run_batch, "load_definition", lambda: _definition(("task-a",)))
    monkeypatch.setattr(run_batch, "load_run_records", lambda *_args, **_kwargs: [record])
    monkeypatch.setattr(
        run_batch,
        "_load_failure_diagnostic_events",
        lambda _record: [_diagnostic_event(run_id, 1, "RunCompleted", status="failed")],
    )
    args = run_batch._parser().parse_args(["--instances", "task-a"])

    # When + Then: the command refuses to present an undiagnosable failed count as done.
    with pytest.raises(n1.ExperimentStateError, match="no failure-cause event"):
        run_batch._execute(args)


def test_duplicate_task_manifests_select_earliest_launch(tmp_path: Path) -> None:
    # Given: historical validation runs claiming the same N1 task and replicate.
    runs_root = tmp_path / "runs"
    run_ids = (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    for index, run_id in enumerate(run_ids):
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        manifest = {
            "cell": n1.CELL_ID,
            "exit_status": "success",
            "replicate": 0,
            "run_id": run_id,
            "started_at": f"2026-07-15T00:00:0{index}Z",
            "study_id": n1.STUDY_ID,
            "task": "task-a",
        }
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # When: history and resume state are loaded.
    history = n1.load_run_history(_definition(("task-a",)), repo_root=tmp_path)
    records = n1.load_run_records(_definition(("task-a",)), repo_root=tmp_path)

    # Then: force can observe both launches while resume preserves first-launch semantics.
    assert [record.run_id for record in history] == list(run_ids)
    assert [record.started_at for record in history] == [
        "2026-07-15T00:00:00Z",
        "2026-07-15T00:00:01Z",
    ]
    assert [record.run_id for record in records] == [run_ids[0]]
    assert all((runs_root / run_id).is_dir() for run_id in run_ids)


def test_event_backed_loading_ignores_filesystem_only_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: two manifests, but only the later run exists in the event store.
    runs_root = tmp_path / "runs"
    run_ids = (
        "00000000-0000-0000-0000-000000000011",
        "00000000-0000-0000-0000-000000000012",
    )
    for index, run_id in enumerate(run_ids):
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "run_manifest.json").write_text(
            json.dumps(
                {
                    "cell": n1.CELL_ID,
                    "exit_status": "success",
                    "replicate": 0,
                    "run_id": run_id,
                    "started_at": f"2026-07-15T00:00:0{index}Z",
                    "study_id": n1.STUDY_ID,
                    "task": "task-a",
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(n1, "_event_backed_run_ids", lambda: {run_ids[1]})

    # When: authoritative records are loaded.
    records = n1.load_run_records(
        _definition(("task-a",)),
        repo_root=tmp_path,
        event_backed_only=True,
    )

    # Then: resume selects the event-backed run and preserves both directories.
    assert [record.run_id for record in records] == [run_ids[1]]
    assert all((runs_root / run_id).is_dir() for run_id in run_ids)


def test_prepare_checks_full_toolchain_and_database_contract() -> None:
    # Given: the prepare command's prerequisite and event-store contracts.
    required_tools = set(prepare.REQUIRED_PROGRAMS)
    valid_status = (
        "12|t|t|event_id,aggregate_id,sequence_number,event_type,payload,occurred_at,metadata|t"
    )

    # When/Then: all run/export tools are checked and a valid schema reports its event count.
    assert {"docker", "uv", "xargs"} <= required_tools
    assert {"psql", "pg_dump", "pg_isready"}.isdisjoint(required_tools)
    assert prepare._parse_database_status(valid_status) == 12

    # And: a database without the OCC uniqueness invariant is rejected.
    invalid_status = valid_status[:-1] + "f"
    with pytest.raises(n1.ExperimentError, match="UNIQUE"):
        prepare._parse_database_status(invalid_status)


def test_prepare_event_coverage_counts_runs_not_event_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one run whose aggregate can have many event rows.
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    records = [
        _record(
            "task-a",
            "00000000-0000-0000-0000-000000000021",
            run_dir,
        )
    ]
    captured: list[list[str]] = []

    def checked(argv: list[str], *, label: str) -> subprocess.CompletedProcess[str]:
        del label
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="1\n", stderr="")

    monkeypatch.setattr(
        prepare,
        "postgres_exec_argv",
        lambda program, *args: ["docker", "exec", "postgres-main", program, *args],
    )
    monkeypatch.setattr(prepare, "_checked_run", checked)

    # When: preparation verifies event coverage.
    prepare._verify_run_events(records)

    # Then: SQL counts covered expected aggregates, not joined event rows.
    query = captured[0][-1]
    assert "WHERE EXISTS" in query
    assert "JOIN events" not in query


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


def test_n1_commands_do_not_use_external_secret_storage() -> None:
    # Given: every operator-facing N1 command.
    paths = [
        N1_DIR / "setup.py",
        N1_DIR / "run.py",
        N1_DIR / "export.py",
        N1_DIR / "prepare_n1_experiment.py",
        N1_DIR / "run_batch.py",
        N1_DIR / "export_data.py",
    ]

    # When: their credential behavior is inspected.
    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths).lower()

    # Then: the handoff does not depend on an external secret store or deployment env.
    assert "bitwarden" not in combined
    assert "bw " not in combined
    assert "source deployment/.env" not in combined


def test_setup_keeps_openai_key_in_ignored_study_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an N1-local environment and a process-provided key during setup.
    setup = _load_script_module("n1_handoff_setup", N1_DIR / "setup.py")
    study_env = tmp_path / "n1" / ".env"
    example_env = tmp_path / "n1" / ".env.example"
    compose_env = tmp_path / "deployment" / ".env"
    study_env.parent.mkdir()
    compose_env.parent.mkdir()
    example_env.write_text(
        "OPENAI_API_KEY=\n"
        "HOST_PROJECT_ROOT=\n"
        "POSTGRES_HOST=127.0.0.1\n"
        "POSTGRES_PORT=55432\n"
        "POSTGRES_USER=arise\n"
        "POSTGRES_DB=arise_events\n",
        encoding="utf-8",
    )
    compose_env.write_text(
        "COMPOSE_ONLY=1\nOPENAI_API_KEY=remove-me\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(setup, "N1_ENV", study_env)
    monkeypatch.setattr(setup, "N1_ENV_EXAMPLE", example_env)
    monkeypatch.setattr(setup, "COMPOSE_ENV", compose_env)
    monkeypatch.setattr(setup, "_database_port", lambda _docker: "6000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")

    # When: setup creates the private env from the committed template and stores the key.
    setup._write_environment("unused")
    setup._ensure_openai_key()

    # Then: only the private study env receives the key.
    assert study_env.stat().st_mode & 0o777 == 0o600
    study_text = study_env.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-test-only" in study_text
    assert f"HOST_PROJECT_ROOT={setup.ROOT_DIR}" in study_text
    assert "POSTGRES_PORT=6000" in study_text
    compose_text = compose_env.read_text(encoding="utf-8")
    assert "COMPOSE_ONLY=1" in compose_text
    assert "OPENAI_API_KEY" not in compose_text
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "experiments/n1-secbench-full/.env"],  # noqa: S607
        cwd=REPO_ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    example_ignored = subprocess.run(
        [  # noqa: S607
            "git",
            "check-ignore",
            "--quiet",
            "experiments/n1-secbench-full/.env.example",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    assert example_ignored.returncode == 1


def test_run_loads_openai_key_only_from_study_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an N1-local environment containing the one permitted persisted key.
    runner = _load_script_module("n1_handoff_run", N1_DIR / "run.py")
    study_env = tmp_path / ".env"
    study_env.write_text(
        "POSTGRES_HOST=127.0.0.1\nOPENAI_API_KEY=sk-test-only\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "ENV_FILE", study_env)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # When: the handoff runner loads its environment.
    runner._load_environment()
    runner._load_openai_key()

    # Then: it receives the study key but still rejects every other persisted secret.
    assert os.environ["OPENAI_API_KEY"] == "sk-test-only"
    study_env.write_text("ANTHROPIC_API_KEY=not-allowed\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(runner.RunError, match="only N1 host settings"):
        runner._load_environment()


def test_run_streams_child_output_to_an_independent_host_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Given: a child command that writes to both stdout and stderr.
    runner = _load_script_module("n1_handoff_run_stream", N1_DIR / "run.py")
    log_path = tmp_path / "run.log"
    monkeypatch.setattr(runner, "ROOT_DIR", tmp_path)

    # When: the host wrapper supervises the child.
    with log_path.open("xb", buffering=0) as log_stream:
        runner._stream_process(
            [
                sys.executable,
                "-c",
                "import sys; print('child stdout'); print('child stderr', file=sys.stderr)",
            ],
            log_stream=log_stream,
        )

    # Then: combined child diagnostics remain live on the terminal and are persisted.
    terminal = capfd.readouterr().out
    saved = log_path.read_text(encoding="utf-8")
    assert "child stdout" in terminal
    assert "child stderr" in terminal
    assert "child stdout" in saved
    assert "child stderr" in saved
    assert all(line.startswith("20") for line in saved.splitlines())


def test_run_preserves_child_diagnostics_before_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Given: a child that emits its failure reason before returning nonzero.
    runner = _load_script_module("n1_handoff_run_failure", N1_DIR / "run.py")
    log_path = tmp_path / "failed-run.log"
    monkeypatch.setattr(runner, "ROOT_DIR", tmp_path)

    # When: the host wrapper observes the failed process.
    with (
        log_path.open("xb", buffering=0) as log_stream,
        pytest.raises(subprocess.CalledProcessError) as failure,
    ):
        runner._stream_process(
            [
                sys.executable,
                "-c",
                "print('intentional child failure'); raise SystemExit(23)",
            ],
            log_stream=log_stream,
        )

    # Then: the exit status and preceding diagnostics are both retained.
    assert failure.value.returncode == 23
    assert "intentional child failure" in capfd.readouterr().out
    assert "intentional child failure" in log_path.read_text(encoding="utf-8")


def test_run_creates_one_automatic_log_around_prepare_and_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a colleague-facing smoke invocation with isolated child commands.
    runner = _load_script_module("n1_handoff_run_log", N1_DIR / "run.py")
    log_dir = tmp_path / "_run_logs"
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(runner, "LOG_DIR", log_dir)
    monkeypatch.setattr(runner, "_load_environment", lambda: None)
    monkeypatch.setattr(runner, "_load_openai_key", lambda: None)

    def fake_run(
        script: str,
        arguments: list[str] | None = None,
        *,
        log_stream: BinaryIO,
    ) -> None:
        calls.append((script, list(arguments or [])))
        log_stream.write(f"child: {script}\n".encode())

    monkeypatch.setattr(runner, "_run", fake_run)

    # When: the wrapper prepares and launches the smoke scope.
    result = runner.main(["--smoke", "--parallel", "1"])

    # Then: one ignored host log contains wrapper and child output without touching events.
    logs = list(log_dir.glob("run-*.log"))
    assert result == 0
    assert len(logs) == 1
    assert logs[0].stat().st_mode & 0o777 == 0o600
    saved = logs[0].read_text(encoding="utf-8")
    assert "run log:" in saved
    assert "run arguments: --parallel 1 --instances gpac.cve-2024-0322" in saved
    assert "starting prepare_n1_experiment.py" in saved
    assert "child: prepare_n1_experiment.py" in saved
    assert "finished prepare_n1_experiment.py" in saved
    assert "starting run_batch.py" in saved
    assert "child: run_batch.py" in saved
    assert "finished run_batch.py" in saved
    assert "run complete:" in saved
    assert calls == [
        ("prepare_n1_experiment.py", []),
        (
            "run_batch.py",
            ["--parallel", "1", "--instances", "gpac.cve-2024-0322"],
        ),
    ]


def test_handoff_readme_exposes_setup_model_run_and_export() -> None:
    # Given: the colleague-facing handoff document.
    readme = (N1_DIR / "README.md").read_text(encoding="utf-8")
    lowered = readme.lower()

    # When: its public workflow is inspected.
    headings = [line for line in readme.splitlines() if line.startswith("## ")]

    # Then: it exposes the four operator actions and no image mechanics.
    assert headings == [
        "## 1. Environment and OpenAI key",
        "## 2. Change the model (optional)",
        "## 3. Run",
        "## 4. Export",
    ]
    assert "experiments/n1-secbench-full/setup.py" in readme
    assert "experiments/n1-secbench-full/.env" in readme
    assert ".env.example" in readme
    assert "asks once" in readme
    assert "study-only" in lowered
    assert "worker.model" in readme
    assert "experiments/n1-secbench-full/run.py" in readme
    assert "experiments/n1-secbench-full/export.py" in readme
    for hidden_detail in (
        "docker",
        "cheshire0814",
        "build-all-images",
        "docker pull",
        "docker push",
    ):
        assert hidden_detail not in lowered


def test_run_and_export_wrappers_have_separate_responsibilities() -> None:
    # Given: the separate run and export commands exposed by the handoff README.
    wrapper = (N1_DIR / "run.py").read_text(encoding="utf-8")
    exporter = (N1_DIR / "export.py").read_text(encoding="utf-8")

    # When/Then: run prepares and executes, while export only delegates to export_data.
    prepare_at = wrapper.index("prepare_n1_experiment.py")
    run_at = wrapper.index("run_batch.py")
    assert prepare_at < run_at
    assert "export_data.py" not in wrapper
    assert "export_data.py" in exporter
    assert "prepare_n1_experiment.py" not in exporter
    assert "run_batch.py" not in exporter
    assert "docker" not in wrapper.lower()
    assert "docker" not in exporter.lower()


def test_secbench_images_link_a_cve_independent_payload() -> None:
    # Given: the payload, thin consumer, lock, and local fallback build definitions.
    payload = (REPO_ROOT / "deployment/secbench-tools-payload.Dockerfile").read_text(
        encoding="utf-8"
    )
    consumer = (REPO_ROOT / "deployment/secbench-tools.Dockerfile").read_text(encoding="utf-8")
    builder = (REPO_ROOT / "deployment/build-secbench-tools.sh").read_text(encoding="utf-8")
    mcp_python = (REPO_ROOT / "deployment/secbench-mcp-python").read_text(encoding="utf-8")
    payload_lock = (REPO_ROOT / "deployment/secbench-tools-payload.lock").read_text(
        encoding="utf-8"
    )

    # When/Then: the published artifact is scratch-based and excludes repository source.
    assert "FROM scratch" in payload
    assert "/opt/arise-node" in payload
    assert "/opt/arise-mcp" in payload
    assert "/opt/arise-python" in payload
    assert "/src" not in payload
    assert "/testcase" not in payload
    assert "security_tools_server.py" not in payload
    assert "secbench-mcp-python" not in payload

    for native_tool in ("valgrind", "gdb", "cppcheck", "strace", "ltrace", "cflow", "jq"):
        assert native_tool in payload

    # And: every local CVE build links that payload without repeating package installs.
    assert "FROM ${SHARED_TOOLS_IMAGE} AS shared_tools" in consumer
    assert consumer.count("COPY --link --from=shared_tools") == 1
    assert "FROM scratch AS local_mcp_server" in consumer
    assert "COPY --link --from=local_mcp_server" in consumer
    assert "plugins/security/mcp/security_tools_server.py" in consumer
    assert "exec /opt/arise-python/bin/python3.12" in mcp_python
    assert "exec /usr/local/bin/python3.12" not in mcp_python
    assert "FROM ${BASE_IMAGE}" in consumer
    for repeated_install in ("apt-get", "npm install", "pip install"):
        assert repeated_install not in consumer
    assert '--build-arg "SHARED_TOOLS_IMAGE=$SHARED_TOOLS_IMAGE"' in builder
    assert 'docker pull --platform linux/amd64 "$SHARED_TOOLS_IMAGE"' in builder
    assert "SHARED_TOOLS_TAG=cheshire0814/secb-tools:payload-focal-amd64-v2" in payload_lock

    # And: the unsafe reference-CVE manifest graft implementation is gone.
    assert not (REPO_ROOT / "plugins/security/toolchain_graft.py").exists()
    assert not (REPO_ROOT / "deployment/publish-secbench-tools.sh").exists()


def test_bulk_image_provisioning_prefers_registry_cache(tmp_path: Path) -> None:
    # Given: no local image and a registry cache hit.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    tagged_image = tmp_path / "tagged.image"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [ "$1:$2:$3" = "image:inspect:--format" ]; then
  if [ -f "$TAGGED_IMAGE" ]; then
    printf 'amd64|application/vnd.docker.distribution.manifest.v2+json|payload-focal-amd64-v2\\n'
    exit 0
  fi
  exit 1
fi
if [ "$1:$2" = "image:rm" ]; then exit 0; fi
if [ "$1" = "pull" ]; then exit 0; fi
if [ "$1" = "tag" ]; then touch "$TAGGED_IMAGE"; exit 0; fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fixture = REPO_ROOT / "plugins/security/tests/fixtures/gpac.cve-2024-0322.json"
    env = os.environ.copy()
    env.update(
        {
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TAGGED_IMAGE": str(tagged_image),
            "TOOLS_IMAGE_REGISTRY": "cache.example",
        }
    )

    # When: the hidden bulk provisioner handles that fixture.
    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", "deployment/build-all-images.sh", str(fixture)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: it pulls and aliases the cache without invoking a local image build.
    commands = docker_log.read_text(encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    assert (
        "pull --platform linux/amd64 cache.example/secb-tools:gpac.cve-2024-0322-patch"
    ) in commands
    assert (
        "tag cache.example/secb-tools:gpac.cve-2024-0322-patch secb-tools:gpac.cve-2024-0322-patch"
    ) in commands
    assert not any(line.startswith(("build ", "buildx ")) for line in commands.splitlines())
    assert "Cached:  1" in completed.stderr
    assert "Built:   0" in completed.stderr


def test_local_image_fallback_pulls_and_consumes_shared_payload(tmp_path: Path) -> None:
    # Given: a local cache miss and a fake Docker daemon that records the fallback build.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    payload_ready = tmp_path / "payload.ready"
    fake_docker = fake_bin / "docker"
    fake_uv = fake_bin / "uv"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [ "$1:$2:$3" = "image:inspect:--format" ]; then
  if [ -f "$PAYLOAD_READY" ]; then printf 'amd64\\n'; exit 0; fi
  exit 1
fi
if [ "$1" = "pull" ]; then touch "$PAYLOAD_READY"; exit 0; fi
if [ "$1:$2" = "image:inspect" ]; then exit 1; fi
case "$1" in
  build|run) exit 0 ;;
esac
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_uv.write_text(
        """#!/bin/sh
if [ "$1:$2:$3" = "run:python:-" ]; then
  printf 'secb-tools:openjpeg.cve-2016-7445-patch\\n'
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = os.environ.copy()
    payload_image = "cache.example/secb-tools:payload-focal-amd64-v2"
    env.update(
        {
            "ARISE_SECBENCH_TOOLS_PAYLOAD_IMAGE": payload_image,
            "DOCKER_LOG": str(docker_log),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PAYLOAD_READY": str(payload_ready),
        }
    )

    # When: one missing CVE image is built through the hidden single-image builder.
    completed = subprocess.run(
        ["/bin/bash", "deployment/build-secbench-tools.sh", "openjpeg.cve-2016-7445"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: the payload is pulled once and passed to the thin Dockerfile build.
    commands = docker_log.read_text(encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    assert f"pull --platform linux/amd64 {payload_image}" in commands
    assert f"--build-arg SHARED_TOOLS_IMAGE={payload_image}" in commands
    assert "run --rm --platform linux/amd64" in commands


def test_bulk_image_provisioning_accepts_verified_mislabeled_cache(
    tmp_path: Path,
) -> None:
    # Given: the first cache generation's OCI index says arm64, but its image is amd64.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    tagged_image = tmp_path / "tagged.image"
    digest = "sha256:" + "a" * 64
    manifest = json.dumps(
        {
            "manifests": [
                {
                    "digest": digest,
                    "platform": {"os": "linux", "architecture": "arm64"},
                }
            ]
        },
        separators=(",", ":"),
    )
    fake_docker = fake_bin / "docker"
    fake_uv = fake_bin / "uv"
    fake_docker.write_text(
        f"""#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [ "$1:$2:$3" = "image:inspect:--format" ]; then
  if [ "$5" = "cache.example/secb-tools@{digest}" ]; then
    printf 'amd64\\n'
    exit 0
  fi
  if [ -f "$TAGGED_IMAGE" ]; then
    printf 'amd64|application/vnd.docker.distribution.manifest.v2+json|payload-focal-amd64-v2\\n'
    exit 0
  fi
  exit 1
fi
if [ "$1:$2" = "pull:--platform" ]; then
  exit 1
fi
if [ "$1:$2" = "manifest:inspect" ]; then
  printf '%s\\n' '{manifest}'
  exit 0
fi
if [ "$1" = "pull" ]; then exit 0; fi
if [ "$1" = "tag" ]; then touch "$TAGGED_IMAGE"; exit 0; fi
if [ "$1:$2" = "image:rm" ]; then
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_uv.write_text(
        """#!/bin/sh
if [ "$1:$2:$3" = "run:python:-c" ]; then
  cat >/dev/null
  printf '%s\\n' "$FAKE_DIGEST"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    fixture = REPO_ROOT / "plugins/security/tests/fixtures/gpac.cve-2024-0322.json"
    env = os.environ.copy()
    env.update(
        {
            "DOCKER_LOG": str(docker_log),
            "FAKE_DIGEST": digest,
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TAGGED_IMAGE": str(tagged_image),
            "TOOLS_IMAGE_REGISTRY": "cache.example",
        }
    )

    # When: the hidden provisioner handles the malformed registry index.
    completed = subprocess.run(  # noqa: S603
        ["/bin/bash", "deployment/build-all-images.sh", str(fixture)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: it selects the child manifest only after verifying its amd64 config.
    commands = docker_log.read_text(encoding="utf-8")
    child = f"cache.example/secb-tools@{digest}"
    assert completed.returncode == 0, completed.stderr
    assert "manifest inspect cache.example/secb-tools:gpac.cve-2024-0322-patch" in commands
    assert f"pull {child}" in commands
    assert f"image inspect --format {{{{.Architecture}}}} {child}" in commands
    assert f"tag {child} secb-tools:gpac.cve-2024-0322-patch" in commands
    assert "Cached:  1" in completed.stderr
    assert "Built:   0" in completed.stderr
