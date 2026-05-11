"""Top-level study harness: `run-ours` subcommand.

Depends only on stdlib + subprocess + yaml plus the cross-study utilities
in ``experiments/shared/scripts``. No import of core/, plugins/, or
infrastructure/ — the harness is pure orchestration around ``main.py run``
(spec §8).

Each ``main.py run`` subprocess receives a unique per-invocation result file
via the ``ARISE_RUN_RESULT_PATH`` env var so the harness can recover its
``boss_id`` without racing on the shared ``.last_run.json`` pointer (which
remains in use for interactive UI workflows only). This makes the matrix
runner safe under ``--parallel >1`` (spec §12 + N-4 audit fix).

Usage::

    python -m experiments.shared.harness run-ours \\
        --study 2026-04-22-demo \\
        --cell B2 \\
        --task gpac.cve-2021-40575 \\
        --replicate 0 \\
        --config experiments/2026-04-22-demo/configs/B2.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.project_events import project_events_to_jsonl
from experiments.shared.scripts.register_run import register_run


# The harness passes a unique result-file path to each subprocess via this env
# var so concurrent `main.py run` invocations cannot misattribute each other's
# run_id. See `presentation/persistence/run_persistence.py:RUN_RESULT_ENV_VAR`.
RUN_RESULT_ENV_VAR = "ARISE_RUN_RESULT_PATH"


logger = logging.getLogger(__name__)


# =============================================================================
# Manifest-driven dispatch (PR 2 onward)
# =============================================================================
# `run_ours` below is the load-bearing implementation of the unified execution
# path. PR 2 introduced runner-registry dispatch (`experiments/shared/runners/aris.py`)
# so cells resolve to a runner via `runners.get(cell.runner)` and the runner
# calls back into `run_ours`. PR 4b folded flat-mode into the same pipeline,
# and PR 5 deleted the legacy `run_baseline` path entirely — every cell (A or
# B) now goes through `main.py run` with `orchestration.mode` selecting flat
# vs. hierarchical dispatch inside the bootstrap composition root.

# =============================================================================
# Dataset and study helpers
# =============================================================================


def _study_dir(study_id: str) -> Path:
    return get_repo_root() / "experiments" / study_id


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"expected YAML file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"expected YAML mapping at {path}, got {type(data).__name__}")
    return data


def _load_study_manifest(study_id: str) -> dict[str, Any]:
    return _load_yaml(_study_dir(study_id) / "manifest.yaml")


def _load_dataset(study_id: str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = manifest or _load_study_manifest(study_id)
    dataset_rel = manifest.get("dataset", "dataset.yaml")
    dataset_path = _study_dir(study_id) / str(dataset_rel)
    return _load_yaml(dataset_path)


def ensure_task_coverage(dataset: dict[str, Any]) -> dict[str, Path]:
    """Pre-flight: every task the harness might run must have a source.paths entry.

    The "task set" is the union of ``default_cves`` and every
    ``per_cell_overrides.<cell>.subset`` entry. Each override subset must itself
    be a subset of ``default_cves`` (spec §6) — we raise a clear ValueError
    listing offending slugs when that invariant is violated so the harness
    aborts before launching any subprocess.

    Returns a mapping of task slug → resolved Path for the context file.
    """
    defaults: list[str] = list(dataset.get("default_cves") or [])
    default_set = set(defaults)
    overrides = dataset.get("per_cell_overrides") or {}

    # Verify each override subset is a subset of default_cves before we accept
    # it into the task set. Reports all cells that violate the rule at once so
    # authors don't have to fix them one at a time.
    offending: dict[str, list[str]] = {}
    task_set: set[str] = set(defaults)
    for cell, override in overrides.items():
        if not isinstance(override, dict):
            continue
        subset = override.get("subset")
        if subset is None:
            continue
        subset_list = list(subset)
        out_of_scope = [slug for slug in subset_list if slug not in default_set]
        if out_of_scope:
            offending[cell] = out_of_scope
        task_set.update(subset_list)
    if offending:
        raise ValueError(
            "per_cell_overrides[*].subset must be a subset of default_cves; "
            f"offending cells: {offending}"
        )

    source = dataset.get("source") or {}
    paths = list(source.get("paths") or [])

    coverage: dict[str, Path] = {}
    for task in sorted(task_set):
        match = _match_path_for_task(task, paths)
        if match is None:
            raise ValueError(
                f"dataset.source.paths has no entry matching task {task!r}; found paths: {paths}"
            )
        resolved = get_repo_root() / match
        if not resolved.is_file():
            raise FileNotFoundError(
                f"source path {match} for task {task!r} does not exist at {resolved}"
            )
        coverage[task] = resolved
    return coverage


def _match_path_for_task(task: str, paths: list[str]) -> str | None:
    """Pick the source.paths entry whose filename encodes the task slug.

    Exact stem matches win first so prefix-related slugs (e.g.
    `foo.cve-2021-1234` vs. `foo.cve-2021-12345`) are never misrouted; only
    when no exact match exists do we fall back to substring matching.
    """
    normalized = task.replace(".", "-").lower()

    # First pass: exact stem match (dot-vs-dash drift tolerant).
    for path in paths:
        stem = Path(path).stem.lower().replace(".", "-")
        if normalized == stem:
            return path

    # Second pass: substring fallback, only if no exact match exists.
    for path in paths:
        stem = Path(path).stem.lower().replace(".", "-")
        if normalized in stem:
            return path
    return None


def resolve_effective_cves(dataset: dict[str, Any], cell: str) -> list[str]:
    defaults: list[str] = list(dataset.get("default_cves") or [])
    overrides = dataset.get("per_cell_overrides") or {}
    cell_override = overrides.get(cell) or {}
    subset = cell_override.get("subset")
    return list(subset) if subset is not None else defaults


def _validate_cell_declared(manifest: dict[str, Any], cell: str) -> None:
    """Pre-flight: confirm ``cell`` is declared in the study manifest.

    Silent fallback to ``default_cves`` when the cell is unknown would let the
    subprocess run against the wrong scope, strand a run_id under
    ``runs/<uuid>/``, and only fail at the later `register_run` step (spec §6).
    We fail fast instead so no expensive work precedes the error.
    """
    cells = manifest.get("cells") or {}
    if not isinstance(cells, dict) or cell not in cells:
        declared = ", ".join(sorted(cells)) if isinstance(cells, dict) else "<none>"
        raise ValueError(f"cell {cell!r} is not declared in study.cells (declared: {declared})")


# =============================================================================
# `run-ours` path
# =============================================================================


def _read_run_result(result_path: Path) -> UUID:
    """Read the per-invocation run-result JSON written by ``cli.py``.

    The path is unique per ``main.py run`` invocation (created by the harness
    via ``tempfile.mkstemp``) so concurrent matrix dispatches cannot stomp
    on each other's run_id — the shared ``.last_run.json`` pointer suffers
    exactly that race under ``--parallel >1``. The harness pre-creates the
    file as an empty placeholder so the subprocess can atomically replace
    it; an empty payload therefore means the subprocess never wrote.
    """
    if not result_path.is_file():
        raise FileNotFoundError(
            f"missing {result_path}; `main.py run` must have failed before writing it"
        )
    text = result_path.read_text(encoding="utf-8").strip()
    if not text:
        raise FileNotFoundError(
            f"empty run-result at {result_path}; `main.py run` exited before writing it"
        )
    data = json.loads(text)
    try:
        return UUID(data["boss_id"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{result_path} has invalid/missing boss_id: {data}") from exc


def _invoke_main_py(
    *,
    config: Path,
    task: str,
    context_file: Path,
    result_path: Path,
    python_bin: str | None = None,
) -> int:
    """Run ``python main.py -c <config> run <task> --domain security --domain-context-file <path>``.

    `-c` MUST come BEFORE the `run` subcommand (top-level flag, see
    bootstrap/bootstrap.py:67). The subprocess inherits ``ARISE_RUN_RESULT_PATH``
    pointing at ``result_path`` so the CLI can emit a per-invocation
    ``{boss_id, status}`` payload there. Returns the subprocess exit code.
    """
    python = python_bin or sys.executable
    main_py = get_repo_root() / "main.py"
    cmd = [
        python,
        str(main_py),
        "-c",
        str(config),
        "run",
        task,
        "--domain",
        "security",
        "--domain-context-file",
        str(context_file),
    ]
    env = {**os.environ, RUN_RESULT_ENV_VAR: str(result_path)}
    logger.info("invoking: %s", " ".join(cmd))
    completed = subprocess.run(  # noqa: S603
        cmd, check=False, cwd=get_repo_root(), env=env
    )
    return completed.returncode


def run_ours(
    *,
    study_id: str,
    cell: str,
    task: str,
    replicate: int,
    config: Path,
    runs_root: Path | None = None,
) -> UUID:
    """Execute an A/B run through `main.py run` and enroll it in the study."""
    manifest = _load_study_manifest(study_id)
    _validate_cell_declared(manifest, cell)
    dataset = _load_dataset(study_id, manifest=manifest)

    coverage = ensure_task_coverage(dataset)
    effective = resolve_effective_cves(dataset, cell)
    if task not in effective:
        raise ValueError(
            f"task {task!r} is not in the effective CVE set for cell {cell!r}: {effective}"
        )

    context_file = coverage[task]
    pool = runs_root or _runs_root(config)
    pool.mkdir(parents=True, exist_ok=True)
    # Per-invocation result file: a unique path per `main.py run` subprocess
    # so concurrent matrix jobs cannot misattribute each other's run_id.
    # `mkstemp` atomically creates the file; the subprocess overwrites it
    # via the atomic-write helper in run_persistence.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".run-result-{cell}-{task}-", suffix=".json", dir=str(pool)
    )
    os.close(fd)
    result_path = Path(tmp_name)

    try:
        exit_code = _invoke_main_py(
            config=config,
            task=task,
            context_file=context_file,
            result_path=result_path,
        )
        # `main.py run` exits non-zero on failed/timeout runs, but the run manifest
        # still carries the outcome. We do NOT bail out here — enroll the run so
        # its exit_status is discoverable. A raise happens only if the result
        # file wasn't written by this subprocess (meaning the run died before any
        # artifacts landed on disk).
        if exit_code != 0:
            logger.warning("`main.py run` exited with %d; continuing enrollment", exit_code)

        run_id = _read_run_result(result_path)
    finally:
        result_path.unlink(missing_ok=True)

    # Write events.jsonl + stamp the per-run manifest with cell/task/replicate.
    # Pass the pinned config so the projection queries the same Postgres
    # instance `main.py run` just wrote to. The study manifest is design-only
    # (PR 1): the enrollment roster is regenerated by collect.py. The run is
    # still enrolled on projection failure but with projection_status=failed
    # so collect.py can exclude it from rollups (audit N-2).
    events_path = pool / str(run_id) / "events.jsonl"
    projection_status = "ok"
    try:
        asyncio.run(
            project_events_to_jsonl(
                run_id=run_id,
                output_path=events_path,
                config_path=config,
            )
        )
    except Exception:
        projection_status = "failed"
        logger.exception(
            "event projection failed for run_id=%s; enrolling with projection_status=failed",
            run_id,
        )

    register_run(
        study_id=study_id,
        run_id=run_id,
        cell=cell,
        task=task,
        replicate=replicate,
        output_directory=pool,
        projection_status=projection_status,
    )
    return run_id


def _runs_root(config: Path | None = None) -> Path:
    """Return the pool root that `main.py run` will write into."""
    if config is None:
        return get_repo_root() / "runs"
    try:
        from config.overlay import resolve_overlay

        materialized = resolve_overlay(config, repo_root=get_repo_root())
        raw_output = ((materialized.get("output") or {}).get("directory"))
        if isinstance(raw_output, str) and raw_output.strip():
            candidate = Path(raw_output)
            return candidate if candidate.is_absolute() else get_repo_root() / candidate
    except Exception:
        logger.exception(
            "failed to resolve output.directory from %s; falling back to runs/",
            config,
        )
    return get_repo_root() / "runs"


# =============================================================================
# CLI
# =============================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="experiments-harness",
        description="Run an A/B experiment cell and enroll the result into a study.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ours = sub.add_parser("run-ours", help="Invoke `main.py run` and enroll it")
    ours.add_argument("--study", required=True)
    ours.add_argument("--cell", required=True)
    ours.add_argument("--task", required=True)
    ours.add_argument("--replicate", type=int, required=True)
    ours.add_argument("--config", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)

    try:
        if args.command == "run-ours":
            run_ours(
                study_id=args.study,
                cell=args.cell,
                task=args.task,
                replicate=args.replicate,
                config=args.config,
            )
        else:
            logger.error("unknown subcommand: %s", args.command)
            return 2
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
