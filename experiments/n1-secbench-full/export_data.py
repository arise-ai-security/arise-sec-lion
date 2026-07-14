#!/usr/bin/env python3
"""Export all local N1 run artifacts and PostgreSQL events for handoff."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from _n1_experiment import (
    CELL_ID,
    EXPECTED_TASK_COUNT,
    REPO_ROOT,
    STUDY_ID,
    ExperimentError,
    RunRecord,
    experiment_lock,
    load_definition,
    load_run_records,
    require_environment,
    require_program,
    sha256_file,
    subprocess_environment,
)
from experiments.shared.scripts.collect import collect_study


logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / f"{STUDY_ID}-export",
        help=f"handoff directory (default: ~/{STUDY_ID}-export)",
    )
    return parser


def _write_runs_archive(records: list[RunRecord], target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with tarfile.open(temporary, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for number, record in enumerate(records, start=1):
                archive.add(record.run_dir, arcname=f"runs/{record.run_id}")
                if number % 10 == 0 or number == len(records):
                    logger.info("[export] archived %d/%d runs", number, len(records))
        temporary.replace(target)
    except (OSError, tarfile.TarError):
        temporary.unlink(missing_ok=True)
        raise


def _write_json(data: dict[str, object], target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _git_sha() -> str:
    git = require_program("git")
    result = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _dump_events(target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.unlink(missing_ok=True)
    try:
        result = subprocess.run(  # noqa: S603
            [str(REPO_ROOT / "scripts" / "dump_events.sh"), str(temporary)],
            cwd=REPO_ROOT,
            env=subprocess_environment(),
            check=False,
        )
        if result.returncode != 0:
            raise ExperimentError("PostgreSQL event export failed")
        temporary.replace(target)
    except (ExperimentError, OSError):
        temporary.unlink(missing_ok=True)
        raise


def _copy_enrollment(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def _write_checksums(paths: list[Path], target: Path) -> None:
    lines = [f"{sha256_file(path)}  {path.name}" for path in paths]
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)


def _execute(output: Path) -> None:
    require_environment(["POSTGRES_PASSWORD"])
    for program in ("bash", "git", "pg_dump", "psql", "gzip", "du", "date"):
        require_program(program)

    definition = load_definition()
    records = load_run_records(definition)
    if not records:
        raise ExperimentError("no N1 runs exist to export")

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    logger.info("[export 1/5] Collect enrollment for %d local runs", len(records))
    enrollment_source = collect_study(STUDY_ID, pool_roots=[REPO_ROOT / "runs"])
    enrollment_target = output / "enrollment.lock.yaml"
    _copy_enrollment(enrollment_source, enrollment_target)

    logger.info("[export 2/5] Archive N1 run directories")
    runs_target = output / "runs.tar.gz"
    _write_runs_archive(records, runs_target)

    logger.info("[export 3/5] Dump this dedicated host's event database")
    events_target = output / "events.sql.gz"
    _dump_events(events_target)

    logger.info("[export 4/5] Write handoff manifest")
    manifest_target = output / "export_manifest.json"
    manifest: dict[str, object] = {
        "cell": CELL_ID,
        "complete_dataset": len(records) == EXPECTED_TASK_COUNT,
        "database_scope": "all events on this dedicated experiment host",
        "expected_tasks": EXPECTED_TASK_COUNT,
        "exported_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "run_count": len(records),
        "runs": [
            {
                "exit_status": record.exit_status,
                "replicate": record.replicate,
                "run_id": record.run_id,
                "task": record.task,
            }
            for record in records
        ],
        "study_id": STUDY_ID,
    }
    _write_json(manifest, manifest_target)

    logger.info("[export 5/5] Write SHA-256 checksums")
    checksum_target = output / "SHA256SUMS"
    _write_checksums(
        [runs_target, events_target, enrollment_target, manifest_target],
        checksum_target,
    )
    logger.info("done: share the entire directory %s", output)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parser().parse_args(argv)
    try:
        with experiment_lock():
            _execute(args.output)
    except (ExperimentError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        logger.error("export failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
