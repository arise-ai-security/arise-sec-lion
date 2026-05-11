"""Run an entire study's matrix end-to-end.

Iterates the cartesian product of (cells x tasks x replicates) declared
in the study's manifest, dispatches each via the appropriate runner, and
on completion runs the existing collect → render → validate pipeline.
Writes a ``matrix-summary.md`` detailing per-cell success/failure and the
list of failed jobs with their error messages.

Usage::

    uv run python -m experiments.shared.scripts.run_matrix \\
        --study <study-id> \\
        [--cells A1,B2] [--tasks gpac.cve-2021-40575] \\
        [--replicates 3] [--parallel 1] [--continue-on-error] \\
        [--no-render]

Defaults: every declared cell and task, ``replicates`` from the manifest
(falling back to 1), ``--parallel 1`` (sequential), and
``--continue-on-error`` so a single failed cell does not abort the matrix.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from experiments.shared import harness, runners
from experiments.shared.scripts import collect
from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.validate_manifest import validate_manifest
from experiments.shared.scripts.write_report import write_md


if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID


logger = logging.getLogger(__name__)


_MATRIX_TEMPLATE_REL = "experiments/shared/templates/matrix-summary.md.j2"
_MATRIX_OUTPUT_NAME = "matrix-summary.md"


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

    results = _dispatch_jobs(
        jobs,
        manifest=manifest,
        dataset=dataset,
        repo_root=repo_root,
        parallel=args.parallel,
        continue_on_error=args.continue_on_error,
    )

    # Post-execution pipeline: collect → render → validate. ``--no-render``
    # skips the per-study render/validate step (useful for partial reruns
    # where the study scripts haven't been wired up yet).
    collect.collect_study(args.study)
    if not args.no_render:
        _render_and_validate(args.study, repo_root=repo_root)
    _write_matrix_summary(
        args.study,
        results=results,
        replicates=replicates,
        cells_filter=cells_filter,
        tasks_filter=tasks_filter,
        repo_root=repo_root,
    )

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

    ``parallel=1`` is sequential. Higher values use a ThreadPoolExecutor
    capped at ``parallel``. ``continue_on_error`` captures exceptions and
    keeps going; otherwise the first raised error propagates.
    """
    coverage = harness.ensure_task_coverage(dataset)

    if parallel <= 1:
        return [_run_one(j, manifest, coverage, repo_root, continue_on_error) for j in jobs]

    results: list[JobResult] = []
    with ThreadPoolExecutor(max_workers=parallel) as executor:
        futures = {
            executor.submit(_run_one, job, manifest, coverage, repo_root, continue_on_error): job
            for job in jobs
        }
        for future in as_completed(futures):
            results.append(future.result())
    # Restore deterministic order so the matrix-summary table matches
    # the enumeration order rather than completion order.
    results.sort(key=lambda r: (r.cell, r.task, r.replicate))
    return results


def _run_one(
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
        run_id = runner.run(
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

    # ``harness.run_ours`` returns a run_id even when ``main.py run`` exits
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
# Post-execution pipeline
# =============================================================================


def _render_and_validate(study_id: str, *, repo_root: Path) -> None:
    """Run the study-local render/validate scripts as subprocesses.

    Per-study scripts (``scripts/collect.py``, ``scripts/plot_success.py``,
    ``scripts/render_report.py``) use module-level path constants tied to
    their on-disk location. Importing them dynamically would activate them
    in the wrong context, so we shell out via ``python -m`` and let each
    script enforce its own preconditions.
    """
    study_dir = repo_root / "experiments" / study_id
    scripts_dir = study_dir / "scripts"

    # Order matters: collect produces tables/summary.csv, plot_success draws
    # the figure that render_report references, render_report writes report.md
    # and finally we re-validate everything.
    scripts: list[tuple[str, list[str]]] = []
    for filename in ("collect.py", "plot_success.py", "render_report.py"):
        candidate = scripts_dir / filename
        if candidate.is_file():
            scripts.append((filename, [sys.executable, str(candidate)]))

    for filename, cmd in scripts:
        logger.info("[%s] running %s", study_id, filename)
        completed = subprocess.run(cmd, check=False, cwd=repo_root)  # noqa: S603
        if completed.returncode != 0:
            raise RuntimeError(f"study script {filename} exited {completed.returncode}")

    validate_cmd = [
        sys.executable,
        "-m",
        "experiments.shared.scripts.validate_reports",
        "--study",
        study_id,
    ]
    logger.info("[%s] validating reports", study_id)
    completed = subprocess.run(validate_cmd, check=False, cwd=repo_root)  # noqa: S603
    if completed.returncode != 0:
        raise RuntimeError(f"validate_reports exited {completed.returncode}")


def _write_matrix_summary(
    study_id: str,
    *,
    results: list[JobResult],
    replicates: int,
    cells_filter: set[str] | None,
    tasks_filter: set[str] | None,
    repo_root: Path,
) -> Path:
    """Render and write ``reports/matrix-summary.md`` with scripts-first provenance."""
    succeeded = [r for r in results if r.status == "succeeded"]
    failed = [r for r in results if r.status == "failed"]

    per_cell_index: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = per_cell_index.setdefault(result.cell, {"succeeded": 0, "failed": 0})
        bucket[result.status] += 1
    per_cell = [
        {"cell": cell, "succeeded": counts["succeeded"], "failed": counts["failed"]}
        for cell, counts in sorted(per_cell_index.items())
    ]

    failed_jobs_view = [
        {
            "cell": r.cell,
            "task": r.task,
            "replicate": r.replicate,
            "error": (r.error or "").replace("|", "\\|").replace("\n", " "),
        }
        for r in failed
    ]

    template_abs = repo_root / _MATRIX_TEMPLATE_REL
    if not template_abs.is_file():
        raise FileNotFoundError(f"matrix-summary template missing: {_MATRIX_TEMPLATE_REL}")

    # Lazy import: jinja2 isn't a stdlib module and would otherwise penalize
    # the --help path that doesn't render anything.
    import jinja2

    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_abs.parent)),
        autoescape=False,  # noqa: S701 - markdown output, not HTML
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_abs.name)
    body = template.render(
        study_id=study_id,
        rendered_at=_utcnow_iso(),
        total_jobs=len(results),
        succeeded_count=len(succeeded),
        failed_count=len(failed),
        replicates=replicates,
        cells_filter=", ".join(sorted(cells_filter)) if cells_filter else "all",
        tasks_filter=", ".join(sorted(tasks_filter)) if tasks_filter else "all",
        per_cell=per_cell,
        failed_jobs=failed_jobs_view,
    )

    output_rel = f"experiments/{study_id}/reports/{_MATRIX_OUTPUT_NAME}"
    script_rel = "experiments/shared/scripts/run_matrix.py"

    return write_md(
        path=output_rel,
        content=body,
        script=script_rel,
        template=_MATRIX_TEMPLATE_REL,
        inputs=[],
    )


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


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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
        help="Max concurrent dispatches. >1 uses threads; mind Docker/LLM limits.",
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
        "--no-render",
        action="store_true",
        help="Skip the per-study render/validate step after dispatch.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and list selected jobs without dispatching workers.",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
