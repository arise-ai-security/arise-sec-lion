#!/usr/bin/env python3
"""Provision, run, checkpoint, and evict every pending task in one N1 shard."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from math import ceil
from pathlib import Path

from _n1_experiment import (
    CELL_ID,
    REPO_ROOT,
    STUDY_ID,
    ExperimentError,
    Shard,
    chunked,
    experiment_lock,
    load_definition,
    load_run_records,
    parse_shard,
    pending_tasks,
    postgres_exec_argv,
    postgres_connection,
    require_environment,
    require_program,
    subprocess_environment,
    tasks_for_shard,
)


logger = logging.getLogger(__name__)


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
    instances = tuple(
        instance.strip() for instance in value.split(",") if instance.strip()
    )
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

    final_records = (
        load_run_records(definition, event_backed_only=True) if remaining else records
    )
    record_by_task = {
        record.task: record for record in final_records if record.task in selected_tasks
    }
    incomplete = [task for task in selected_tasks if task not in record_by_task]
    if incomplete:
        raise ExperimentError("selected run scope is incomplete: " + ", ".join(incomplete))
    succeeded = sum(
        record.exit_status == "success" for record in record_by_task.values()
    )
    logger.info(
        "done: shard=%s completed=%d succeeded=%d failed=%d",
        args.shard.label,
        len(selected_tasks),
        succeeded,
        len(selected_tasks) - succeeded,
    )


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
