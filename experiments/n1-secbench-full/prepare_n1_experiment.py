#!/usr/bin/env python3
"""Check all N1 prerequisites and initialize its PostgreSQL event store."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from _n1_experiment import (
    REPO_ROOT,
    ExperimentError,
    RunRecord,
    StudyDefinition,
    experiment_lock,
    load_definition,
    load_run_records,
    postgres_connection,
    require_environment,
    require_program,
    subprocess_environment,
)
from config.settings import Settings


logger = logging.getLogger(__name__)

EXPECTED_BRANCH = "experiment/n1-secbench-full"
RECOMMENDED_FREE_GIB = 50
REQUIRED_PROGRAMS = (
    "awk",
    "bash",
    "cut",
    "date",
    "docker",
    "du",
    "git",
    "grep",
    "gzip",
    "mktemp",
    "pg_dump",
    "pg_isready",
    "psql",
    "sed",
    "tr",
    "uv",
    "wc",
    "xargs",
)
REQUIRED_PYTHON_MODULES = ("asyncpg", "openhands.sdk", "openhands.tools")
EXPECTED_EVENT_COLUMNS = (
    "event_id",
    "aggregate_id",
    "sequence_number",
    "event_type",
    "payload",
    "occurred_at",
    "metadata",
)


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        argv,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def _checked_run(argv: list[str], *, label: str) -> subprocess.CompletedProcess[str]:
    result = _run(argv, env=subprocess_environment())
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ExperimentError(f"{label}: {detail}")
    return result


def _check_repository(definition: StudyDefinition) -> str:
    git = require_program("git")
    root = _checked_run([git, "rev-parse", "--show-toplevel"], label="not a Git checkout")
    if Path(root.stdout.strip()).resolve() != REPO_ROOT:
        raise ExperimentError(f"run from repository root {REPO_ROOT}")

    branch = _checked_run([git, "branch", "--show-current"], label="cannot read Git branch")
    if branch.stdout.strip() != EXPECTED_BRANCH:
        raise ExperimentError(f"checkout branch {EXPECTED_BRANCH!r} before running N1")

    dirty = _checked_run(
        [git, "status", "--porcelain", "--untracked-files=no"],
        label="cannot inspect Git status",
    )
    if dirty.stdout.strip():
        raise ExperimentError("commit or restore tracked changes before running N1")

    sha = _checked_run([git, "rev-parse", "HEAD"], label="cannot read Git commit")
    Settings.from_yaml(definition.config_path)
    return sha.stdout.strip()


def _check_python_modules() -> None:
    missing: list[str] = []
    for module in REQUIRED_PYTHON_MODULES:
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            missing.append(module)
    if missing:
        raise ExperimentError(f"missing Python dependencies: {', '.join(missing)}")


def _ensure_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".n1-write-", dir=path, delete=False) as probe:
            probe_path = Path(probe.name)
    except OSError as exc:
        raise ExperimentError(f"directory is not writable: {path}: {exc}") from exc
    finally:
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


def _check_host_prerequisites() -> tuple[str, float]:
    programs = {program: require_program(program) for program in REQUIRED_PROGRAMS}
    docker = programs["docker"]
    if Path("/.dockerenv").exists() or os.environ.get("DOCKER_HOST"):
        host_project_root = os.environ.get("HOST_PROJECT_ROOT", "")
        if not host_project_root or not Path(host_project_root).is_absolute():
            raise ExperimentError(
                "HOST_PROJECT_ROOT must be an absolute host path when using Docker-out-of-Docker"
            )
    _checked_run([docker, "info"], label="Docker daemon is not ready")
    _checked_run([docker, "compose", "version"], label="Docker Compose is unavailable")
    _checked_run([docker, "system", "df"], label="Docker storage is unavailable")

    _ensure_writable_directory(REPO_ROOT / "runs")
    _ensure_writable_directory(REPO_ROOT / "experiments" / "n1-secbench-full" / "reports")
    free_gib = shutil.disk_usage(REPO_ROOT).free / 1024**3
    if free_gib < RECOMMENDED_FREE_GIB:
        logger.warning(
            "prerequisite warning: %.1f GiB free; at least %d GiB is recommended",
            free_gib,
            RECOMMENDED_FREE_GIB,
        )
    return docker, free_gib


def _compose_file() -> Path:
    return REPO_ROOT / "deployment" / "docker-compose.yml"


def _ensure_compose_env_file() -> None:
    env_file = REPO_ROOT / "deployment" / ".env"
    if not env_file.exists():
        env_file.touch(mode=0o600)


def _start_database(docker: str) -> None:
    host, _, _, _ = postgres_connection()
    if host not in {"localhost", "127.0.0.1"}:
        logger.info("[prepare] use external PostgreSQL host %s", host)
        return

    _ensure_compose_env_file()
    _checked_run(
        [docker, "compose", "-f", str(_compose_file()), "config", "--quiet"],
        label="invalid Docker Compose configuration",
    )
    _checked_run(
        [
            docker,
            "compose",
            "-f",
            str(_compose_file()),
            "--profile",
            "local",
            "up",
            "-d",
            "db",
        ],
        label="cannot start PostgreSQL",
    )


def _psql_argv(psql: str) -> list[str]:
    host, port, user, database = postgres_connection()
    return [
        psql,
        "--host",
        host,
        "--port",
        port,
        "--username",
        user,
        "--dbname",
        database,
        "--set",
        "ON_ERROR_STOP=1",
    ]


def _wait_for_database() -> None:
    pg_isready = require_program("pg_isready")
    host, port, user, database = postgres_connection()
    argv = [
        pg_isready,
        "--host",
        host,
        "--port",
        port,
        "--username",
        user,
        "--dbname",
        database,
    ]
    for _ in range(30):
        if _run(argv, env=subprocess_environment()).returncode == 0:
            return
        time.sleep(2)
    raise ExperimentError(f"PostgreSQL is not ready at {host}:{port}/{database}")


def _database_status_query() -> str:
    return """
SELECT
    (SELECT COUNT(*) FROM events),
    has_table_privilege(current_user, 'events', 'SELECT'),
    has_table_privilege(current_user, 'events', 'INSERT'),
    (SELECT string_agg(column_name, ',' ORDER BY ordinal_position)
       FROM information_schema.columns
      WHERE table_schema = 'public' AND table_name = 'events'),
    EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'events'::regclass
           AND contype = 'u'
           AND pg_get_constraintdef(oid) LIKE '%(aggregate_id, sequence_number)%'
    );
""".strip()


def _parse_database_status(stdout: str) -> int:
    fields = stdout.strip().split("|")
    if len(fields) != 5:
        raise ExperimentError(f"unexpected PostgreSQL status response: {stdout.strip()!r}")
    count_text, can_select, can_insert, columns_text, has_occ = fields
    columns = tuple(columns_text.split(","))
    if columns != EXPECTED_EVENT_COLUMNS:
        raise ExperimentError(f"events table has unexpected columns: {columns_text}")
    if can_select != "t" or can_insert != "t":
        raise ExperimentError("database user requires SELECT and INSERT on events")
    if has_occ != "t":
        raise ExperimentError("events table is missing UNIQUE(aggregate_id, sequence_number)")
    try:
        return int(count_text)
    except ValueError as exc:
        raise ExperimentError(f"invalid event count: {count_text!r}") from exc


def _initialize_database() -> int:
    psql = require_program("psql")
    schema = REPO_ROOT / "infrastructure" / "sql" / "create_events_table.sql"
    _checked_run([*_psql_argv(psql), "--file", str(schema)], label="database initialization failed")
    result = _checked_run(
        [
            *_psql_argv(psql),
            "--tuples-only",
            "--no-align",
            "--field-separator",
            "|",
            "--command",
            _database_status_query(),
        ],
        label="database schema verification failed",
    )
    return _parse_database_status(result.stdout)


def _verify_run_events(records: list[RunRecord]) -> None:
    if not records:
        return
    psql = require_program("psql")
    values = ",".join(f"('{record.run_id}'::uuid)" for record in records)
    query = (
        "WITH expected(aggregate_id) AS (VALUES "
        f"{values}) SELECT COUNT(*) FROM expected JOIN events USING (aggregate_id);"
    )
    result = _checked_run(
        [*_psql_argv(psql), "--tuples-only", "--no-align", "--command", query],
        label="cannot verify resumable run events",
    )
    if int(result.stdout.strip()) != len(records):
        raise ExperimentError("one or more run manifests have no matching root events")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _parser().parse_args(argv)
    try:
        with experiment_lock():
            logger.info("[prepare 1/6] Validate the committed 300-task experiment")
            definition = load_definition()
            logger.info("[prepare 2/6] Check keys, branch, config, and Python dependencies")
            require_environment(["OPENAI_API_KEY", "POSTGRES_PASSWORD"])
            git_sha = _check_repository(definition)
            _check_python_modules()
            logger.info("[prepare 3/6] Check host tools, writable paths, Docker, and disk")
            docker, free_gib = _check_host_prerequisites()
            logger.info("[prepare 4/6] Start PostgreSQL")
            _start_database(docker)
            _wait_for_database()
            logger.info("[prepare 5/6] Initialize and verify the event-store schema")
            event_count = _initialize_database()
            logger.info("[prepare 6/6] Validate resumable run and event state")
            records = load_run_records(definition)
            _verify_run_events(records)
    except (ExperimentError, OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.error("prepare failed: %s", exc)
        return 1

    logger.info(
        "ready: commit=%s dataset=%d completed=%d pending=%d events=%d free_disk=%.1fGiB",
        git_sha[:12],
        len(definition.tasks),
        len(records),
        len(definition.tasks) - len(records),
        event_count,
        free_gib,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
