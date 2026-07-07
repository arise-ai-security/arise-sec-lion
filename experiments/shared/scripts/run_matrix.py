"""Run an entire study's matrix end-to-end.

Iterates the cartesian product of (cells x tasks x replicates) declared
in the study's manifest, dispatches each via the appropriate runner, and
on completion regenerates the study's enrollment roster from the runs pool.

Usage::

    uv run python -m experiments.shared.scripts.run_matrix \\
        --study <study-id> \\
        [--cells A1,B2] [--tasks gpac.cve-2021-40575] \\
        [--replicates 3] [--parallel 1] [--continue-on-error]

Defaults: every declared cell and task, ``replicates`` from the manifest
(falling back to 1), ``--parallel 1`` (sequential), and
``--continue-on-error`` so a single failed cell does not abort the matrix.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import yaml

from experiments.shared import harness, runners
from experiments.shared.container_cleanup import _sweep_stale_containers
from experiments.shared.scripts import collect
from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.validate_manifest import validate_manifest


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobResult:
    """Outcome of a single (cell, task, replicate) dispatch."""

    cell: str
    task: str
    replicate: int
    run_id: UUID | None
    status: str  # "succeeded" | "failed"
    error: str | None


def main(argv: list[str] | None = None) -> int:
    """Driver entry point. Returns 0 on full success, 1 if any job failed."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)

    repo_root = get_repo_root()
    manifest = _load_manifest(args.study, repo_root=repo_root)
    validate_manifest(manifest, repo_root=repo_root)
    dataset = _load_dataset(args.study, manifest=manifest, repo_root=repo_root)

    cells_filter = _parse_csv(args.cells)
    tasks_filter = _parse_csv(args.tasks)
    replicates = (
        args.replicates if args.replicates is not None else int(manifest.get("replicates", 1))
    )
    if replicates < 1:
        logger.error("replicates must be >= 1, got %d", replicates)
        return 1

    jobs = _enumerate_jobs(
        manifest=manifest,
        dataset=dataset,
        cells_filter=cells_filter,
        tasks_filter=tasks_filter,
        replicates=replicates,
    )
    if not jobs:
        logger.error("no jobs match the given filters")
        return 1

    logger.info(
        "matrix: %d jobs (cells=%s, tasks=%s, replicates=%d, parallel=%d)",
        len(jobs),
        sorted(cells_filter) if cells_filter else "all",
        sorted(tasks_filter) if tasks_filter else "all",
        replicates,
        args.parallel,
    )
    if args.dry_run:
        for cell, task, replicate in jobs:
            logger.info("dry-run job: cell=%s task=%s replicate=%d", cell, task, replicate)
        logger.info("dry-run complete: %d jobs would run", len(jobs))
        return 0

    pools = _collect_pools_from_manifest(
        manifest, repo_root=repo_root, cells_filter=cells_filter
    )
    _sweep_stale_state(pools)

    results = _dispatch_jobs(
        jobs,
        manifest=manifest,
        dataset=dataset,
        repo_root=repo_root,
        parallel=args.parallel,
        continue_on_error=args.continue_on_error,
    )

    # Post-execution: regenerate the study's enrollment roster (the derived
    # lockfile) from the per-run manifests in the runs pool.
    collect.collect_study(args.study)

    failed = [r for r in results if r.status == "failed"]
    if failed:
        logger.warning("matrix complete: %d/%d jobs failed", len(failed), len(results))
        return 1
    logger.info("matrix complete: %d/%d jobs succeeded", len(results), len(results))
    return 0


# =============================================================================
# Job enumeration and dispatch
# =============================================================================


def _enumerate_jobs(
    *,
    manifest: dict[str, Any],
    dataset: dict[str, Any],
    cells_filter: set[str] | None,
    tasks_filter: set[str] | None,
    replicates: int,
) -> list[tuple[str, str, int]]:
    """Cartesian product of (cell, task, replicate), respecting filters.

    Per-cell scope honours the dataset's ``per_cell_overrides``: a cell
    that subsets ``default_cves`` only enumerates its subset. The tasks
    filter is applied AFTER scope resolution so callers can ask for a
    specific task without worrying which cells include it.
    """
    cells = manifest.get("cells") or {}
    if cells_filter is not None:
        unknown = cells_filter - set(cells)
        if unknown:
            raise ValueError(f"unknown cells in --cells filter: {sorted(unknown)}")

    jobs: list[tuple[str, str, int]] = []
    for cell_name in sorted(cells):
        if cells_filter is not None and cell_name not in cells_filter:
            continue
        cell_tasks = harness.resolve_effective_cves(dataset, cell_name)
        for task in cell_tasks:
            if tasks_filter is not None and task not in tasks_filter:
                continue
            for replicate in range(replicates):
                jobs.append((cell_name, task, replicate))
    return jobs


def _dispatch_jobs(
    jobs: list[tuple[str, str, int]],
    *,
    manifest: dict[str, Any],
    dataset: dict[str, Any],
    repo_root: Path,
    parallel: int,
    continue_on_error: bool,
) -> list[JobResult]:
    """Dispatch each job through the appropriate runner.

    ``parallel=1`` is sequential. Higher values use an asyncio subprocess
    supervisor capped at ``parallel``. Native async runners are awaited
    directly; legacy sync runners run through a compatibility worker thread.
    ``continue_on_error`` captures exceptions and keeps going; otherwise the
    first raised error propagates and pending jobs are cancelled.
    """
    return asyncio.run(
        _dispatch_jobs_async(
            jobs,
            manifest=manifest,
            dataset=dataset,
            repo_root=repo_root,
            parallel=parallel,
            continue_on_error=continue_on_error,
        )
    )


async def _dispatch_jobs_async(
    jobs: list[tuple[str, str, int]],
    *,
    manifest: dict[str, Any],
    dataset: dict[str, Any],
    repo_root: Path,
    parallel: int,
    continue_on_error: bool,
) -> list[JobResult]:
    coverage = harness.ensure_task_coverage(dataset)
    limit = max(1, parallel)

    if limit == 1:
        results: list[JobResult] = []
        for job in jobs:
            results.append(
                await _run_one_async(job, manifest, coverage, repo_root, continue_on_error)
            )
        return results

    semaphore = asyncio.Semaphore(limit)

    async def _guarded(job: tuple[str, str, int]) -> JobResult:
        async with semaphore:
            return await _run_one_async(job, manifest, coverage, repo_root, continue_on_error)

    tasks = [asyncio.create_task(_guarded(job)) for job in jobs]
    try:
        results = await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    # Restore deterministic order so results match the enumeration order
    # rather than completion order.
    results.sort(key=lambda r: (r.cell, r.task, r.replicate))
    return results


async def _run_one_async(
    job: tuple[str, str, int],
    manifest: dict[str, Any],
    coverage: dict[str, Path],
    repo_root: Path,
    continue_on_error: bool,
) -> JobResult:
    """Dispatch a single (cell, task, replicate) tuple through its runner."""
    cell_name, task, replicate = job
    cell_spec = manifest["cells"][cell_name]
    runner = runners.get(cell_spec["runner"])
    config_path = repo_root / "experiments" / manifest["study_id"] / cell_spec["config"]

    context_file = coverage.get(task)
    if context_file is None:
        message = f"no context_file for task {task!r} in cell {cell_name!r}"
        if not continue_on_error:
            raise ValueError(message)
        logger.warning("job (%s, %s, %d) failed: %s", cell_name, task, replicate, message)
        return JobResult(
            cell=cell_name,
            task=task,
            replicate=replicate,
            run_id=None,
            status="failed",
            error=message,
        )

    try:
        run_id = await _invoke_runner(
            runner,
            study_id=manifest["study_id"],
            cell=cell_name,
            task=task,
            replicate=replicate,
            config=config_path,
            context_file=context_file,
        )
    except Exception as exc:
        if not continue_on_error:
            raise
        logger.warning("job (%s, %s, %d) failed: %s", cell_name, task, replicate, exc)
        return JobResult(
            cell=cell_name,
            task=task,
            replicate=replicate,
            run_id=None,
            status="failed",
            error=str(exc),
        )

    # ``harness.run_arise`` returns a run_id even when ``main.py run`` exits
    # non-zero (so the failure is enrolled and discoverable). Reflect that real
    # exit status in the matrix summary instead of falsely reporting success.
    run_status, run_error = _read_run_status(repo_root, manifest["study_id"], run_id)
    return JobResult(
        cell=cell_name,
        task=task,
        replicate=replicate,
        run_id=run_id,
        status=run_status,
        error=run_error,
    )


async def _invoke_runner(runner: Any, **kwargs: object) -> UUID:
    run_async = getattr(runner, "run_async", None)
    if callable(run_async):
        async_runner = cast(Callable[..., Awaitable[UUID]], run_async)
        return await async_runner(**kwargs)
    sync_runner = cast(Callable[..., UUID], runner.run)
    return await asyncio.to_thread(sync_runner, **kwargs)


def _read_run_status(
    repo_root: Path, study_id: str, run_id: UUID
) -> tuple[str, str | None]:
    """Read the run's ``run_manifest.json`` and map ``exit_status`` → matrix status.

    ``main.py run`` writes the manifest's ``exit_status`` after every run
    (success or failure). Matrix-level ``succeeded`` requires a clean inner
    exit; anything else is reported as ``failed`` with the manifest's status
    string as the error.
    """
    del study_id  # reserved for future per-study pool overrides
    import json as _json

    from experiments.shared.scripts.load_runs import default_pool_roots

    candidate: Path | None = None
    for pool in default_pool_roots():
        path = pool / str(run_id) / "run_manifest.json"
        if path.is_file():
            candidate = path
            break
    if candidate is None:
        # Fall back to the conventional <repo>/runs/ pool.
        path = repo_root / "runs" / str(run_id) / "run_manifest.json"
        if path.is_file():
            candidate = path
    if candidate is None:
        # Test fixtures and edge cases (run_id returned without a manifest on
        # disk) trust the runner's UUID return as success. Real runs always
        # produce run_manifest.json, so this only kicks in when the runner is
        # stubbed or the file system isn't observable from this process.
        return "succeeded", None

    try:
        record = _json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        return "failed", f"could not read run_manifest.json: {exc}"
    raw_status = str(record.get("exit_status") or "").lower()
    if raw_status in {"completed", "succeeded", "success"}:
        return "succeeded", None
    if not raw_status:
        return "failed", "run_manifest.json has no exit_status"
    return "failed", f"inner run exit_status={raw_status}"


# =============================================================================
# Helpers
# =============================================================================


def _load_manifest(study_id: str, *, repo_root: Path) -> dict[str, Any]:
    path = repo_root / "experiments" / study_id / "manifest.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"study manifest not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"study manifest is not a YAML mapping: {path}")
    data.setdefault("study_id", study_id)
    return data


def _load_dataset(study_id: str, *, manifest: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    dataset_rel = manifest.get("dataset", "dataset.yaml")
    path = repo_root / "experiments" / study_id / str(dataset_rel)
    if not path.is_file():
        raise FileNotFoundError(f"dataset not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"dataset is not a YAML mapping: {path}")
    return data


def _parse_csv(value: str | None) -> set[str] | None:
    """Parse a comma-separated CLI argument into a set, or None when absent."""
    if value is None:
        return None
    parts = {item.strip() for item in value.split(",") if item.strip()}
    return parts or None


# =============================================================================
# Startup sweep (F.2): remove stale containers + orphan result files
# =============================================================================


_ORPHAN_RESULT_MAX_AGE_SECONDS = 3600


def _collect_pools_from_manifest(
    manifest: dict[str, Any],
    *,
    repo_root: Path,
    cells_filter: set[str] | None,
) -> set[Path]:
    """Return every pool root that may contain stale state from a prior run.

    The default ``<repo>/runs/`` pool is always included; per-cell configs
    may relocate the pool via ``output.directory`` overlay. The sweep is
    scoped to the cells the operator actually selected so we don't reach
    into unrelated cells' pools.
    """
    pools: set[Path] = {repo_root / "runs"}
    cells = manifest.get("cells") or {}
    for cell_name, cell_spec in cells.items():
        if cells_filter is not None and cell_name not in cells_filter:
            continue
        config_rel = cell_spec.get("config") if isinstance(cell_spec, dict) else None
        if not config_rel:
            continue
        config_path = repo_root / "experiments" / manifest["study_id"] / config_rel
        pools.add(_runs_root_for_config(config_path, repo_root=repo_root))
    return pools


def _runs_root_for_config(config_path: Path, *, repo_root: Path) -> Path:
    """Resolve a cell config's ``output.directory`` to an absolute pool root.

    Mirrors ``harness._runs_root`` but stays module-local so the sweep does
    not couple to harness internals. Any failure falls back to the
    conventional ``<repo>/runs/`` pool, matching the harness behaviour.
    """
    try:
        from config.overlay import resolve_overlay

        materialized = resolve_overlay(config_path, repo_root=repo_root)
        raw_output = (materialized.get("output") or {}).get("directory")
        if isinstance(raw_output, str) and raw_output.strip():
            candidate = Path(raw_output)
            return candidate if candidate.is_absolute() else repo_root / candidate
    except Exception:
        logger.warning(
            "startup sweep: failed to resolve output.directory from %s; using default",
            config_path,
        )
    return repo_root / "runs"


def _sweep_stale_state(pools: set[Path]) -> None:
    """Daemon-wide container sweep + per-pool orphan ``.run-result-*.json`` sweep."""
    _sweep_stale_containers()
    for pool in pools:
        _sweep_orphan_result_files(pool)


def _sweep_orphan_result_files(pool: Path) -> None:
    if not pool.exists():
        return
    cutoff = time.time() - _ORPHAN_RESULT_MAX_AGE_SECONDS
    for path in pool.glob(".run-result-*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("startup sweep: failed to unlink %s (%s)", path, exc)


# =============================================================================
# CLI
# =============================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_matrix",
        description="Run an entire study's experiment matrix end-to-end.",
    )
    parser.add_argument("--study", required=True, help="Study id (folder name).")
    parser.add_argument(
        "--cells",
        help="Comma-separated cells to run (default: every declared cell).",
    )
    parser.add_argument(
        "--tasks",
        help="Comma-separated tasks to run (default: every task in scope).",
    )
    parser.add_argument(
        "--replicates",
        type=int,
        help="Replicate count override (default: manifest.replicates or 1).",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Max concurrent dispatches; mind Docker/LLM limits.",
    )
    parser.add_argument(
        "--continue-on-error",
        dest="continue_on_error",
        action="store_true",
        default=True,
        help="Capture per-job failures and continue (default).",
    )
    parser.add_argument(
        "--no-continue-on-error",
        dest="continue_on_error",
        action="store_false",
        help="Abort on the first failure (raises).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and list selected jobs without dispatching workers.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
