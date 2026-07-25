#!/usr/bin/env python3
"""Provision, run, checkpoint, and evict every pending task in one N1 shard."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING
from uuid import UUID

from _n1_experiment import (
    CELL_ID,
    REPO_ROOT,
    STUDY_ID,
    ExperimentError,
    ExperimentStateError,
    RunRecord,
    Shard,
    chunked,
    experiment_lock,
    load_definition,
    load_run_records,
    parse_shard,
    pending_tasks,
    postgres_connection,
    postgres_exec_argv,
    require_environment,
    require_program,
    subprocess_environment,
    tasks_for_shard,
)


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

# Scope by aggregate instead of filtering on the secondary event_type index:
# failure reporting must still work when that index is unhealthy. The CASE keeps
# prompt/thought payloads out of the subprocess response before Python sees it.
_FAILURE_DIAGNOSTIC_SQL = """
WITH RECURSIVE hierarchy(aggregate_id) AS (
    SELECT 'RUN_ID_PLACEHOLDER'::uuid
    UNION
    SELECT created.aggregate_id
    FROM events AS created
    JOIN hierarchy AS parent
      ON created.payload->>'parent_id' = parent.aggregate_id::text
    WHERE created.event_type = 'AgentCreated'
)
SELECT json_build_object(
    'event_id', event.event_id::text,
    'aggregate_id', event.aggregate_id::text,
    'sequence_number', event.sequence_number,
    'event_type', event.event_type,
    'occurred_at', event.occurred_at,
    'payload', CASE
        WHEN event.event_type = ANY (ARRAY[
            'AgentCreated',
            'TaskAssigned',
            'RunStarted',
            'RunCompleted',
            'StatusChanged',
            'WorkFailed',
            'VerificationFailed',
            'DecisionInfeasible',
            'RetryScheduled',
            'FailureDigestRecorded',
            'ChildFailed',
            'ProcedureExecutionFinished',
            'PhaseGateRecorded',
            'LimitEnforced',
            'AgentExecutionStarted',
            'AgentExecutionFinished',
            'OperationStarted',
            'OperationFinished',
            'CodeGenerationStarted'
        ])
        THEN event.payload
        ELSE '{}'::jsonb
    END
)::text
FROM events AS event
JOIN hierarchy USING (aggregate_id)
ORDER BY event.occurred_at, event.aggregate_id, event.sequence_number;
"""

_DIAGNOSTIC_FIELDS: dict[str, tuple[str, ...]] = {
    "AgentCreated": ("role", "parent_id"),
    "TaskAssigned": ("task_description",),
    "RunStarted": ("task_description",),
    "StatusChanged": ("old_status", "new_status", "reason"),
    "WorkFailed": ("reason",),
    "VerificationFailed": (
        "failed_stage",
        "feedback",
        "stages_passed",
        "score",
    ),
    "DecisionInfeasible": ("reason", "minimum_subtasks", "minimum_depth"),
    "RetryScheduled": ("attempt", "reason", "escalated_model"),
    "FailureDigestRecorded": ("source", "digest"),
    "ChildFailed": ("child_id", "child_task", "reason", "digest"),
    "ProcedureExecutionFinished": ("procedure_ref", "success", "summary"),
    "PhaseGateRecorded": ("phase", "passed", "reason", "evidence_references"),
    "LimitEnforced": (
        "limit_type",
        "limit_value",
        "attempted_value",
        "action_taken",
    ),
    "AgentExecutionStarted": ("role", "depth"),
    "AgentExecutionFinished": ("role", "status", "duration_seconds"),
    "OperationStarted": ("operation_type",),
    "OperationFinished": ("operation_type", "duration_seconds"),
    "CodeGenerationStarted": ("tool_name",),
    "RunCompleted": (
        "status",
        "duration_seconds",
        "total_agents",
        "completed_agents",
        "failed_agents",
    ),
}
_CAUSE_EVENT_TYPES = {
    "WorkFailed",
    "VerificationFailed",
    "DecisionInfeasible",
    "FailureDigestRecorded",
    "ChildFailed",
}
_MANIFEST_DIAGNOSTIC_FIELDS = (
    "started_at",
    "ended_at",
    "summary_available",
    "models",
    "tokens",
    "costs_by_model",
    "cost_incomplete",
    "cost_completeness_rate",
    "deliverables",
)
_SECRET_NAME_SUFFIXES = ("_KEY", "_PASSWORD", "_SECRET", "_TOKEN", "_DSN")
_SECRET_CONNECTION_NAMES = {
    "DATABASE_URL",
    "DB_URL",
    "INVOKEAI_DB_URL",
    "POSTGRES_URL",
}
_BEARER_SECRET = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_INLINE_SECRET = re.compile(
    r"(?i)(\b(?:api[_ -]?key|password|secret|token)\b\s*[:=]\s*)"
    r"[^\s,;\"']{8,}"
)
_OPENAI_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


@dataclass(frozen=True)
class FailureDiagnosticEvent:
    """One safe-to-render event from a failed run's persisted hierarchy."""

    event_id: str
    aggregate_id: str
    sequence_number: int
    event_type: str
    occurred_at: str
    payload: dict[str, object]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _shard(value: str) -> Shard:
    try:
        return parse_shard(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _instances(value: str) -> tuple[str, ...]:
    instances = tuple(instance.strip() for instance in value.split(",") if instance.strip())
    if not instances:
        raise argparse.ArgumentTypeError("must contain at least one instance ID")
    if len(set(instances)) != len(instances):
        raise argparse.ArgumentTypeError("instance IDs must be unique")
    return instances


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=30,
        help="progress checkpoint size (default: 30)",
    )
    parser.add_argument(
        "--parallel",
        type=_positive_int,
        default=2,
        help="maximum simultaneous image provisioning and N1 runs (default: 2)",
    )
    parser.add_argument(
        "--shard",
        type=_shard,
        default=parse_shard("1/1"),
        help="disjoint host shard as INDEX/COUNT (default: 1/1)",
    )
    parser.add_argument(
        "--instances",
        "--tasks",
        dest="instances",
        type=_instances,
        help="comma-separated SEC-bench instance IDs; run only this explicit subset",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="retain SEC-bench images instead of freeing disk after each wave",
    )
    return parser


def _selected_tasks(
    definition_tasks: tuple[str, ...],
    *,
    shard: Shard,
    instances: tuple[str, ...] | None,
) -> list[str]:
    if instances is None:
        return tasks_for_shard(definition_tasks, shard)
    if shard.label != "1/1":
        raise ExperimentError("--instances cannot be combined with --shard")
    known = set(definition_tasks)
    unknown = [instance for instance in instances if instance not in known]
    if unknown:
        raise ExperimentError("unknown SEC-bench instance(s): " + ", ".join(unknown))
    return list(instances)


def _run(argv: list[str], *, quiet: bool = False) -> int:
    result = subprocess.run(  # noqa: S603
        argv,
        cwd=REPO_ROOT,
        env=subprocess_environment(),
        check=False,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )
    return result.returncode


def _require_runtime() -> str:
    require_environment(["OPENAI_API_KEY"])
    docker = require_program("docker")
    require_program("bash")

    if _run([docker, "info"], quiet=True) != 0:
        raise ExperimentError("Docker is not ready; run prepare_n1_experiment.py")
    _, _, user, database = postgres_connection()
    if (
        _run(
            postgres_exec_argv(
                "pg_isready",
                "--username",
                user,
                "--dbname",
                database,
            ),
            quiet=True,
        )
        != 0
    ):
        raise ExperimentError("PostgreSQL is not ready; run prepare_n1_experiment.py")
    return docker


def _provision_wave(tasks: list[str], fixtures: dict[str, Path], parallel: int) -> None:
    fixture_paths = [str(fixtures[task]) for task in tasks]
    argv = [
        str(REPO_ROOT / "deployment" / "build-all-images.sh"),
        "--parallel",
        str(parallel),
        "--fail-fast",
        *fixture_paths,
    ]
    if _run(argv) != 0:
        raise ExperimentError(f"image provisioning failed for: {', '.join(tasks)}")


def _run_wave(tasks: list[str], parallel: int) -> int:
    return _run(
        [
            sys.executable,
            "-m",
            "experiments.shared.scripts.run_matrix",
            "--study",
            STUDY_ID,
            "--cells",
            CELL_ID,
            "--tasks",
            ",".join(tasks),
            "--parallel",
            str(parallel),
            "--replicates",
            "1",
        ]
    )


def _parse_failure_diagnostic_events(stdout: str) -> list[FailureDiagnosticEvent]:
    events: list[FailureDiagnosticEvent] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExperimentStateError(
                f"failure diagnostic query returned invalid JSON on line {line_number}"
            ) from exc
        if not isinstance(raw, dict):
            raise ExperimentStateError(
                f"failure diagnostic query returned a non-object on line {line_number}"
            )
        event_id = raw.get("event_id")
        aggregate_id = raw.get("aggregate_id")
        sequence_number = raw.get("sequence_number")
        event_type = raw.get("event_type")
        occurred_at = raw.get("occurred_at")
        payload = raw.get("payload")
        if (
            not isinstance(event_id, str)
            or not isinstance(aggregate_id, str)
            or not isinstance(sequence_number, int)
            or isinstance(sequence_number, bool)
            or not isinstance(event_type, str)
            or not isinstance(occurred_at, str)
            or not isinstance(payload, dict)
        ):
            raise ExperimentStateError(
                f"failure diagnostic query returned an invalid event on line {line_number}"
            )
        if event_type not in _DIAGNOSTIC_FIELDS:
            continue
        events.append(
            FailureDiagnosticEvent(
                event_id=event_id,
                aggregate_id=aggregate_id,
                sequence_number=sequence_number,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload,
            )
        )
    return events


def _load_failure_diagnostic_events(record: RunRecord) -> list[FailureDiagnosticEvent]:
    try:
        run_id = str(UUID(record.run_id))
    except ValueError as exc:
        raise ExperimentStateError(
            f"cannot diagnose N1 run with invalid UUID {record.run_id!r}"
        ) from exc
    _, _, user, database = postgres_connection()
    query = _FAILURE_DIAGNOSTIC_SQL.replace("RUN_ID_PLACEHOLDER", run_id)
    result = subprocess.run(  # noqa: S603
        postgres_exec_argv(
            "psql",
            "--username",
            user,
            "--dbname",
            database,
            "--tuples-only",
            "--no-align",
            "--command",
            query,
        ),
        cwd=REPO_ROOT,
        env=subprocess_environment(),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ExperimentStateError(
            f"cannot read failure events for N1 run {record.run_id}: {detail}"
        )
    return _parse_failure_diagnostic_events(result.stdout)


def _read_failure_manifest(record: RunRecord) -> dict[str, object]:
    path = record.run_dir / "run_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExperimentStateError(
            f"cannot read failure manifest for N1 run {record.run_id}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ExperimentStateError(
            f"failure manifest for N1 run {record.run_id} is not a JSON object"
        )
    return manifest


def _redact_diagnostic_text(text: str) -> str:
    redacted = text
    for name, value in os.environ.items():
        if (name.endswith(_SECRET_NAME_SUFFIXES) or name in _SECRET_CONNECTION_NAMES) and len(
            value
        ) >= 8:
            redacted = redacted.replace(value, "<redacted>")
    redacted = _BEARER_SECRET.sub(r"\1<redacted>", redacted)
    redacted = _INLINE_SECRET.sub(r"\1<redacted>", redacted)
    return _OPENAI_SECRET.sub("<redacted>", redacted)


def _diagnostic_value(value: object) -> str:
    return _redact_diagnostic_text(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _event_details(event: FailureDiagnosticEvent) -> str:
    fields = _DIAGNOSTIC_FIELDS.get(event.event_type, ())
    return " ".join(
        f"{field}={_diagnostic_value(event.payload[field])}"
        for field in fields
        if field in event.payload and event.payload[field] not in (None, "", [], {})
    )


def _is_failure_cause(event: FailureDiagnosticEvent) -> bool:
    if event.event_type in _CAUSE_EVENT_TYPES:
        return True
    if event.event_type == "StatusChanged":
        return event.payload.get("new_status") in {"failed", "timed_out", "cancelled"}
    if event.event_type == "ProcedureExecutionFinished":
        return event.payload.get("success") is False
    if event.event_type == "PhaseGateRecorded":
        return event.payload.get("passed") is False
    return False


def _log_failure_diagnostics(record: RunRecord) -> None:
    manifest_path = record.run_dir / "run_manifest.json"
    manifest = _read_failure_manifest(record)
    events = _load_failure_diagnostic_events(record)
    causes = [event for event in events if _is_failure_cause(event)]

    logger.error(
        "[failure] begin task=%s run_id=%s exit_status=%s",
        record.task,
        record.run_id,
        record.exit_status,
    )
    logger.error("[failure] manifest=%s", manifest_path)
    logger.error(
        "[failure] testcase=%s exists=%s",
        record.run_dir / "testcase",
        (record.run_dir / "testcase").is_dir(),
    )
    logger.error(
        "[failure] source_snapshot=%s exists=%s",
        record.run_dir / "src",
        (record.run_dir / "src").is_dir(),
    )
    for field in _MANIFEST_DIAGNOSTIC_FIELDS:
        if field in manifest:
            logger.error("[failure] manifest.%s=%s", field, _diagnostic_value(manifest[field]))
    logger.error(
        "[failure] diagnostic_events=%d failure_causes=%d",
        len(events),
        len(causes),
    )
    for index, cause in enumerate(causes, start=1):
        logger.error(
            "[failure] cause=%d/%d aggregate_id=%s type=%s %s",
            index,
            len(causes),
            cause.aggregate_id,
            cause.event_type,
            _event_details(cause),
        )
    for event in events:
        details = _event_details(event)
        logger.error(
            "[failure] event occurred_at=%s aggregate_id=%s sequence=%d event_id=%s type=%s%s",
            event.occurred_at,
            event.aggregate_id,
            event.sequence_number,
            event.event_id,
            event.event_type,
            f" {details}" if details else "",
        )
    logger.error("[failure] end task=%s run_id=%s", record.task, record.run_id)

    if not causes:
        raise ExperimentStateError(
            "persisted diagnostics for failed N1 run "
            f"{record.run_id} contain no failure-cause event"
        )


def _evict_wave(
    docker: str,
    tasks: list[str],
    base_images: dict[str, str],
) -> None:
    tool_images = [f"secb-tools:{task}-patch" for task in tasks]
    images = [*tool_images, *(base_images[task] for task in tasks)]
    result = subprocess.run(  # noqa: S603
        [docker, "image", "rm", *images],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        logger.warning("image eviction was partial: %s", result.stderr.strip())
    result = subprocess.run(  # noqa: S603
        [docker, "builder", "prune", "--force"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        logger.warning("build-cache cleanup failed: %s", result.stderr.strip())


def _execute(args: argparse.Namespace) -> None:
    definition = load_definition()
    records = load_run_records(definition, event_backed_only=True)
    selected_tasks = _selected_tasks(
        definition.tasks,
        shard=args.shard,
        instances=args.instances,
    )
    remaining = pending_tasks(selected_tasks, records)
    total_batches = ceil(len(remaining) / args.batch_size) if remaining else 0
    docker = _require_runtime() if remaining else ""

    logger.info(
        "[run] shard=%s tasks=%d completed=%d pending=%d parallel=%d",
        args.shard.label,
        len(selected_tasks),
        len(selected_tasks) - len(remaining),
        len(remaining),
        args.parallel,
    )

    for batch_number, batch in enumerate(chunked(remaining, args.batch_size), start=1):
        waves = list(chunked(batch, args.parallel))
        for wave_number, wave in enumerate(waves, start=1):
            prefix = f"[batch {batch_number}/{total_batches} wave {wave_number}/{len(waves)}]"
            logger.info("%s provision %d task image(s)", prefix, len(wave))
            try:
                _provision_wave(wave, definition.fixtures, len(wave))
                logger.info("%s run %d task(s)", prefix, len(wave))
                run_returncode = _run_wave(wave, len(wave))
                current_records = load_run_records(definition, event_backed_only=True)
                missing = pending_tasks(wave, current_records)
                if missing:
                    raise ExperimentError(
                        "runner did not create manifests for: " + ", ".join(missing)
                    )
                if run_returncode != 0:
                    logger.info("%s recorded one or more failed experimental outcomes", prefix)
            finally:
                if not args.keep_images:
                    logger.info("%s evict images and build cache", prefix)
                    _evict_wave(docker, wave, definition.base_images)

        current_records = load_run_records(definition, event_backed_only=True)
        completed = len(selected_tasks) - len(pending_tasks(selected_tasks, current_records))
        logger.info(
            "[batch %d/%d] checkpoint: %d/%d shard tasks complete",
            batch_number,
            total_batches,
            completed,
            len(selected_tasks),
        )

    final_records = load_run_records(definition, event_backed_only=True) if remaining else records
    record_by_task = {
        record.task: record for record in final_records if record.task in selected_tasks
    }
    incomplete = [task for task in selected_tasks if task not in record_by_task]
    if incomplete:
        raise ExperimentError("selected run scope is incomplete: " + ", ".join(incomplete))
    succeeded = sum(record.exit_status == "success" for record in record_by_task.values())
    failed_records = [
        record
        for task, record in record_by_task.items()
        if task in selected_tasks and record.exit_status != "success"
    ]
    logger.info(
        "done: shard=%s completed=%d succeeded=%d failed=%d",
        args.shard.label,
        len(selected_tasks),
        succeeded,
        len(failed_records),
    )
    for record in failed_records:
        _log_failure_diagnostics(record)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parser().parse_args(argv)
    try:
        with experiment_lock():
            _execute(args)
    except KeyboardInterrupt:
        logger.error("run interrupted; rerun the same command to resume")
        return 130
    except (ExperimentError, OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.error("run failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
