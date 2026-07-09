"""Top-level study harness: `run-arise` subcommand.

Depends only on stdlib + yaml plus the cross-study utilities
in ``experiments/shared/scripts``. No import of core/, plugins/, or
infrastructure/ — the harness is pure orchestration around ``main.py run``
(spec §8).

Each ``main.py run`` subprocess receives a unique per-invocation result file
via the ``ARISE_RUN_RESULT_PATH`` env var so the harness can recover its
``boss_id`` without racing on the shared ``.last_run.json`` pointer (which
remains in use for interactive UI workflows only). This makes the matrix
runner safe under ``--parallel >1`` (spec §12 + N-4 audit fix).

Usage::

    python -m experiments.shared.harness run-arise \\
        --study 2026-04-22-demo \\
        --cell B2 \\
        --task gpac.cve-2021-40575 \\
        --replicate 0 \\
        --config experiments/2026-04-22-demo/configs/B2.yaml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.register_run import register_run
from experiments.shared.subprocess_runner import (
    _SUBPROCESS_TIMEOUT_EXIT_CODE,
    _invoke_main_py,
    _invoke_main_py_async,
)


logger = logging.getLogger(__name__)


class MainPyTimeoutError(RuntimeError):
    """Raised when the parent kills a hung ``main.py run`` child."""


@dataclass(frozen=True)
class _PreparedRunArise:
    context_file: Path
    pool: Path
    result_path: Path


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
# `run-arise` path
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


def _result_file_has_payload(result_path: Path) -> bool:
    try:
        return bool(result_path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _read_run_result_after_exit(
    *,
    exit_code: int,
    result_path: Path,
) -> UUID:
    # `main.py run` exits non-zero on failed/timeout runs, but the run manifest
    # still carries the outcome. We do NOT bail out here — enroll the run so
    # its exit_status is discoverable. A raise happens only if the result
    # file wasn't written by this subprocess (meaning the run died before any
    # artifacts landed on disk).
    if exit_code != 0:
        logger.warning("`main.py run` exited with %d; continuing enrollment", exit_code)
    if exit_code == _SUBPROCESS_TIMEOUT_EXIT_CODE and not _result_file_has_payload(result_path):
        raise MainPyTimeoutError(
            "`main.py run` timed out and was killed before writing its run-result file"
        )
    return _read_run_result(result_path)


def _register_completed_run(
    *,
    study_id: str,
    cell: str,
    task: str,
    replicate: int,
    pool: Path,
    run_id: UUID,
) -> None:
    # Stamp the per-run manifest with study/cell/task/replicate. The Postgres
    # event store is the source of truth for the event stream (read directly by
    # analysis tooling), so no events.jsonl is projected to disk. The study
    # manifest is design-only (PR 1): the enrollment roster is regenerated by
    # collect.py from the per-run manifests.
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell=cell,
        task=task,
        replicate=replicate,
        output_directory=pool,
    )


def _prepare_run_arise(
    *,
    study_id: str,
    cell: str,
    task: str,
    config: Path,
    runs_root: Path | None,
) -> _PreparedRunArise:
    manifest = _load_study_manifest(study_id)
    _validate_cell_declared(manifest, cell)
    dataset = _load_dataset(study_id, manifest=manifest)

    coverage = ensure_task_coverage(dataset)
    effective = resolve_effective_cves(dataset, cell)
    if task not in effective:
        raise ValueError(
            f"task {task!r} is not in the effective CVE set for cell {cell!r}: {effective}"
        )

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
    return _PreparedRunArise(
        context_file=coverage[task],
        pool=pool,
        result_path=Path(tmp_name),
    )


def run_arise(
    *,
    study_id: str,
    cell: str,
    task: str,
    replicate: int,
    config: Path,
    runs_root: Path | None = None,
) -> UUID:
    """Execute an A/B run through `main.py run` and enroll it in the study."""
    prepared = _prepare_run_arise(
        study_id=study_id,
        cell=cell,
        task=task,
        config=config,
        runs_root=runs_root,
    )

    try:
        exit_code = _invoke_main_py(
            config=config,
            task=task,
            context_file=prepared.context_file,
            result_path=prepared.result_path,
        )
        run_id = _read_run_result_after_exit(
            exit_code=exit_code,
            result_path=prepared.result_path,
        )
    finally:
        prepared.result_path.unlink(missing_ok=True)

    _register_completed_run(
        study_id=study_id,
        cell=cell,
        task=task,
        replicate=replicate,
        pool=prepared.pool,
        run_id=run_id,
    )
    return run_id


async def run_arise_async(
    *,
    study_id: str,
    cell: str,
    task: str,
    replicate: int,
    config: Path,
    runs_root: Path | None = None,
) -> UUID:
    """Async variant used by matrix dispatch to supervise subprocesses directly."""
    prepared = _prepare_run_arise(
        study_id=study_id,
        cell=cell,
        task=task,
        config=config,
        runs_root=runs_root,
    )

    try:
        exit_code = await _invoke_main_py_async(
            config=config,
            task=task,
            context_file=prepared.context_file,
            result_path=prepared.result_path,
        )
        run_id = _read_run_result_after_exit(
            exit_code=exit_code,
            result_path=prepared.result_path,
        )
    finally:
        prepared.result_path.unlink(missing_ok=True)

    _register_completed_run(
        study_id=study_id,
        cell=cell,
        task=task,
        replicate=replicate,
        pool=prepared.pool,
        run_id=run_id,
    )
    return run_id


def _runs_root(config: Path | None = None) -> Path:
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

    arise = sub.add_parser("run-arise", help="Invoke `main.py run` and enroll it")
    arise.add_argument("--study", required=True)
    arise.add_argument("--cell", required=True)
    arise.add_argument("--task", required=True)
    arise.add_argument("--replicate", type=int, required=True)
    arise.add_argument("--config", type=Path, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)

    try:
        if args.command == "run-arise":
            run_arise(
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
