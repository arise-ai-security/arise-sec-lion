"""Shared validation and resume state for the N1 SEC-bench full run."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.overlay import resolve_overlay  # noqa: E402
from experiments.shared.harness import ensure_task_coverage  # noqa: E402
from experiments.shared.scripts.load_runs import load_runs  # noqa: E402


STUDY_ID = "n1-secbench-full"
CELL_ID = "N1"
EXPECTED_TASK_COUNT = 300
POSTGRES_CONTAINER = "postgres-main"

T = TypeVar("T")
logger = logging.getLogger(__name__)


class ExperimentError(RuntimeError):
    """Base error for the N1 experiment commands."""


class ExperimentDefinitionError(ExperimentError):
    """The committed study no longer matches the N1 experiment contract."""


class ExperimentStateError(ExperimentError):
    """Existing local experiment state is ambiguous or unsafe to resume."""


@dataclass(frozen=True)
class StudyDefinition:
    tasks: tuple[str, ...]
    fixtures: dict[str, Path]
    base_images: dict[str, str]
    config_path: Path
    manifest_path: Path
    dataset_path: Path


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task: str
    exit_status: str
    replicate: int
    run_dir: Path


@dataclass(frozen=True)
class Shard:
    index: int
    count: int

    @property
    def label(self) -> str:
        return f"{self.index + 1}/{self.count}"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ExperimentDefinitionError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExperimentDefinitionError(f"{path} must contain a YAML mapping")
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExperimentDefinitionError(message)


def _validate_n1_control_config(config: dict[str, Any]) -> None:
    """Require the full-run cell to remain an unassisted OpenHands control."""
    orchestration = config.get("orchestration") or {}
    worker = config.get("worker") or {}
    output = config.get("output") or {}
    openhands = ((worker.get("tool_params") or {}).get("openhands") or {})
    _require(orchestration.get("mode") == "flat", "N1 orchestration.mode must be flat")
    _require(
        orchestration.get("procedural_dispatch") is False,
        "N1 Host procedural dispatch must be disabled explicitly",
    )
    _require(orchestration.get("skip_judge") is True, "N1 must skip the LLM judge")
    _require(worker.get("tool") == "openhands", "N1 worker tool must be OpenHands")
    _require(openhands.get("enable_subagents") is False, "N1 OpenHands subagents must be disabled")
    _require(output.get("directory") == "./runs", "N1 output.directory must be ./runs")


def _base_image(fixture: dict[str, Any], *, fixture_path: Path) -> str:
    override = fixture.get("docker_image_override")
    if isinstance(override, str) and override:
        return override

    instance_id = fixture.get("instance_id")
    project = fixture.get("project_name")
    if not isinstance(instance_id, str):
        raise ExperimentDefinitionError(f"{fixture_path}: instance_id must be a string")
    if not isinstance(project, str):
        raise ExperimentDefinitionError(f"{fixture_path}: project_name must be a string")
    parts = instance_id.split(".", maxsplit=1)
    _require(len(parts) == 2, f"{fixture_path}: instance_id must contain a project prefix")
    return f"hwiwonlee/secb.eval.x86_64.{project}.{parts[1]}:patch"


def load_definition(*, repo_root: Path = REPO_ROOT) -> StudyDefinition:
    """Load and strictly validate the committed 300-task N1 definition."""
    study_dir = repo_root / "experiments" / STUDY_ID
    manifest_path = study_dir / "manifest.yaml"
    manifest = _load_yaml(manifest_path)

    _require(manifest.get("study_id") == STUDY_ID, f"manifest study_id must be {STUDY_ID!r}")
    _require(manifest.get("replicates") == 1, "N1 full must use exactly one replicate")
    cells = manifest.get("cells")
    if not isinstance(cells, dict) or set(cells) != {CELL_ID}:
        raise ExperimentDefinitionError("manifest must define only N1")
    cell = cells[CELL_ID]
    if not isinstance(cell, dict):
        raise ExperimentDefinitionError("manifest cells.N1 must be a mapping")
    _require(cell.get("runner") == "arise", "N1 must use the arise runner")

    dataset_rel = manifest.get("dataset")
    if not isinstance(dataset_rel, str):
        raise ExperimentDefinitionError("manifest dataset must be a path")
    dataset_path = study_dir / dataset_rel
    dataset = _load_yaml(dataset_path)
    metadata = dataset.get("metadata")
    if not isinstance(metadata, dict):
        raise ExperimentDefinitionError("dataset metadata must be a mapping")
    _require(metadata.get("source") == "SEC-bench/SEC-bench", "dataset source must be SEC-bench")
    _require(metadata.get("split") == "eval", "dataset split must be eval")

    raw_tasks = dataset.get("default_cves")
    if not isinstance(raw_tasks, list):
        raise ExperimentDefinitionError("dataset default_cves must be a list")
    _require(all(isinstance(task, str) for task in raw_tasks), "all task IDs must be strings")
    tasks = tuple(task for task in raw_tasks if isinstance(task, str))
    _require(len(tasks) == EXPECTED_TASK_COUNT, f"dataset must contain {EXPECTED_TASK_COUNT} tasks")
    _require(len(set(tasks)) == len(tasks), "dataset contains duplicate task IDs")
    _require(dataset.get("per_cell_overrides") == {}, "N1 full must not subset the dataset")

    try:
        fixtures = ensure_task_coverage(dataset)
    except (FileNotFoundError, ValueError) as exc:
        raise ExperimentDefinitionError(str(exc)) from exc

    base_images: dict[str, str] = {}
    for task, fixture_path in fixtures.items():
        try:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ExperimentDefinitionError(f"cannot read {fixture_path}: {exc}") from exc
        _require(isinstance(fixture, dict), f"{fixture_path} must contain a JSON object")
        _require(
            fixture.get("instance_id") == task,
            f"{fixture_path}: instance_id must be {task!r}",
        )
        base_images[task] = _base_image(fixture, fixture_path=fixture_path)

    config_rel = cell.get("config")
    if not isinstance(config_rel, str):
        raise ExperimentDefinitionError("manifest cells.N1.config must be a path")
    config_path = study_dir / config_rel
    try:
        config = resolve_overlay(config_path, repo_root=repo_root)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError) as exc:
        raise ExperimentDefinitionError(f"cannot resolve {config_path}: {exc}") from exc

    _validate_n1_control_config(config)

    return StudyDefinition(
        tasks=tasks,
        fixtures=dict(fixtures),
        base_images=base_images,
        config_path=config_path,
        manifest_path=manifest_path,
        dataset_path=dataset_path,
    )


def load_run_records(
    definition: StudyDefinition,
    *,
    repo_root: Path = REPO_ROOT,
    event_backed_only: bool = False,
) -> list[RunRecord]:
    """Load N1 manifests and reject state that cannot be resumed unambiguously."""
    event_backed_ids = _event_backed_run_ids() if event_backed_only else None
    rows = load_runs(
        study_id=STUDY_ID,
        cells=[CELL_ID],
        pool_roots=[repo_root / "runs"],
    )
    known_tasks = set(definition.tasks)
    selected_by_task: dict[str, tuple[tuple[str, str], RunRecord]] = {}
    for row in rows:
        run_id = row.get("run_id")
        task = row.get("task")
        replicate = row.get("replicate", 0)
        exit_status = row.get("exit_status")
        if not isinstance(run_id, str) or not isinstance(task, str):
            raise ExperimentStateError("an N1 run manifest has no string run_id/task")
        try:
            UUID(run_id)
        except ValueError as exc:
            raise ExperimentStateError(f"N1 run has invalid UUID {run_id!r}") from exc
        if event_backed_ids is not None and run_id not in event_backed_ids:
            logger.warning(
                "ignoring filesystem-only run %s for task %s; no PostgreSQL events exist",
                run_id,
                task,
            )
            continue
        if task not in known_tasks:
            raise ExperimentStateError(f"N1 run {run_id} has unknown task {task!r}")
        if replicate != 0:
            raise ExperimentStateError(f"N1 run {run_id} has replicate {replicate}; expected 0")
        if not isinstance(exit_status, str):
            raise ExperimentStateError(f"N1 run {run_id} has no exit_status")
        run_dir = repo_root / "runs" / run_id
        if not run_dir.is_dir():
            raise ExperimentStateError(f"run directory is missing for {run_id}")
        record = RunRecord(
            run_id=run_id,
            task=task,
            exit_status=exit_status,
            replicate=replicate,
            run_dir=run_dir,
        )
        started_at = row.get("started_at")
        selection_key = (started_at if isinstance(started_at, str) else "", run_id)
        previous = selected_by_task.get(task)
        if previous is None or selection_key < previous[0]:
            selected_by_task[task] = (selection_key, record)
        if previous is not None:
            canonical = selected_by_task[task][1].run_id
            logger.warning(
                "task %s has multiple historical runs; using earliest run %s",
                task,
                canonical,
            )
    task_order = {task: index for index, task in enumerate(definition.tasks)}
    records = [item[1] for item in selected_by_task.values()]
    return sorted(records, key=lambda record: task_order[record.task])


def _event_backed_run_ids() -> set[str]:
    """Return aggregate IDs present in the authoritative PostgreSQL event store."""
    _, _, user, database = postgres_connection()
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
            "SELECT DISTINCT aggregate_id::text FROM events;",
        ),
        cwd=REPO_ROOT,
        env=subprocess_environment(),
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ExperimentStateError(f"cannot read PostgreSQL event IDs: {detail}")
    return {line for line in result.stdout.splitlines() if line}


def parse_shard(value: str) -> Shard:
    """Parse a one-based INDEX/COUNT shard identifier."""
    try:
        index_text, count_text = value.split("/", maxsplit=1)
        index = int(index_text)
        count = int(count_text)
    except (ValueError, TypeError) as exc:
        raise ValueError("shard must use INDEX/COUNT, for example 1/10") from exc
    if count < 1 or index < 1 or index > count:
        raise ValueError("shard must satisfy 1 <= INDEX <= COUNT")
    return Shard(index=index - 1, count=count)


def tasks_for_shard(tasks: Sequence[str], shard: Shard) -> list[str]:
    return list(tasks[shard.index :: shard.count])


def pending_tasks(tasks: Sequence[str], records: Sequence[RunRecord]) -> list[str]:
    completed = {record.task for record in records}
    return [task for task in tasks if task not in completed]


def chunked(items: Sequence[T], size: int) -> Iterator[list[T]]:
    if size < 1:
        raise ValueError("chunk size must be at least 1")
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def require_environment(names: Sequence[str]) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ExperimentStateError(f"missing environment variables: {', '.join(missing)}")


def require_program(name: str) -> str:
    path = shutil.which(name)
    if path is not None:
        return path
    raise ExperimentStateError(f"required program is not installed: {name}")


def postgres_exec_argv(program: str, *args: str) -> list[str]:
    """Build a command that runs a PostgreSQL client inside postgres-main."""
    return [require_program("docker"), "exec", POSTGRES_CONTAINER, program, *args]


def postgres_connection() -> tuple[str, str, str, str]:
    return (
        os.environ.get("POSTGRES_HOST", "localhost"),
        os.environ.get("POSTGRES_PORT", "5432"),
        os.environ.get("POSTGRES_USER", "arise"),
        os.environ.get("POSTGRES_DB", "arise_events"),
    )


def subprocess_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("POSTGRES_PASSWORD", None)
    env.pop("PGPASSWORD", None)
    return env


@contextmanager
def experiment_lock(*, repo_root: Path = REPO_ROOT) -> Iterator[None]:
    """Prevent two local N1 commands from mutating/exporting the same run pool."""
    digest = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:12]
    lock_path = Path(tempfile.gettempdir()) / f"arise-{STUDY_ID}-{digest}.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExperimentStateError("another N1 prepare/run/export command is active") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
