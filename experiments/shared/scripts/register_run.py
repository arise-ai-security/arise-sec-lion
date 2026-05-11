"""Stamp experiment-level fields onto a run's `run_manifest.json`.

The runtime writes runtime-level fields (`started_at`, `models`, `tokens`, ...)
into `runs/<run_id>/run_manifest.json` when `main.py run` executes; the
experiment-level fields (`cell`, `task`, `replicate`, `study_id`) are merged
in here by the harness after the subprocess returns, or by a human invoking
this script for a historical run.

This module no longer mutates `experiments/<study>/manifest.yaml`. The
study manifest is design-only (cells, hypothesis, dataset); the enrollment
roster is a derived artifact, regenerated from the runs pool by
``experiments.shared.scripts.collect`` into
``experiments/<study>/reports/enrollment.lock.yaml`` (PR 1 of the
experiments rearchitecture).

Usage::

    python -m experiments.shared.scripts.register_run \
        --study 2026-04-22-naive-vs-cybersec \
        --run-id <uuid> \
        --cell B2 \
        --task gpac.cve-2021-40575 \
        --replicate 0
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import yaml

from experiments.shared.scripts._paths import get_repo_root


if TYPE_CHECKING:
    from collections.abc import Iterable


logger = logging.getLogger(__name__)


def _study_manifest_path(study_id: str) -> Path:
    return get_repo_root() / "experiments" / study_id / "manifest.yaml"


def _runs_root(output_directory: str | Path | None = None) -> Path:
    """Return the flat runs pool root (default: `<repo_root>/runs`)."""
    if output_directory is None:
        return get_repo_root() / "runs"
    return Path(output_directory)


def _run_manifest_path(run_id: UUID, runs_root: Path) -> Path:
    return runs_root / str(run_id) / "run_manifest.json"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"study manifest not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"study manifest is not a mapping: {path}")
    return data


def _prepare_run_manifest_payload(
    run_manifest_path: Path,
    *,
    study_id: str,
    cell: str,
    task: str,
    replicate: int,
    projection_status: str,
) -> dict[str, Any]:
    """Load the run manifest and merge experiment-level fields in memory.

    Does not write anything. Raises FileNotFoundError if the run manifest is
    absent so callers can fail fast. ``attempt`` is preserved alongside
    ``replicate`` for one rename cycle so consumers that still read the old
    field keep working. ``projection_status`` (audit N-2) carries forward
    whether `events.jsonl` was generated successfully (``"ok"``) or the
    projection raised (``"failed"``); `collect.py` excludes failed rows so
    a missing/empty events.jsonl no longer masquerades as a clean zero.
    """
    if not run_manifest_path.is_file():
        raise FileNotFoundError(
            f"run manifest not found: {run_manifest_path}. The run must finish "
            "before it can be enrolled into a study."
        )
    payload = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "study_id": study_id,
            "cell": cell,
            "task": task,
            "replicate": replicate,
            "attempt": replicate,
            "projection_status": projection_status,
        }
    )
    return payload


def _write_run_manifest(run_manifest_path: Path, payload: dict[str, Any]) -> None:
    """Atomically overwrite ``run_manifest_path`` with ``payload`` (tmp + rename)."""
    tmp = run_manifest_path.with_name(run_manifest_path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(run_manifest_path)


def _validate_cell_declared(study: dict[str, Any], cell: str) -> None:
    cells = study.get("cells") or {}
    if not isinstance(cells, dict) or cell not in cells:
        declared = ", ".join(sorted(cells)) if isinstance(cells, dict) else "<none>"
        raise ValueError(f"cell {cell!r} not declared in study.cells (declared: {declared})")


def _validate_task_in_cell_scope(study: dict[str, Any], cell: str, task: str) -> None:
    """Confirm `task` is in the cell's effective CVE set.

    The study's `dataset.yaml` carries `default_cves` and `per_cell_overrides`;
    we resolve the cell's scope and refuse to enroll a run whose task isn't
    in that scope (spec §6 invariant).
    """
    dataset_rel = study.get("dataset")
    if not isinstance(dataset_rel, str):
        # No dataset declared — skip the check (permissive so ad-hoc studies work).
        return

    dataset_path = _study_manifest_path(study.get("study_id", "")).parent / dataset_rel
    if not dataset_path.is_file():
        # Declared but missing is a configuration bug — fail closed so typos
        # don't silently disable the CVE-scope invariant.
        raise FileNotFoundError(
            f"study declares dataset={dataset_rel!r} but the file was not found at {dataset_path}"
        )

    dataset = yaml.safe_load(dataset_path.read_text(encoding="utf-8")) or {}
    defaults: Iterable[str] = dataset.get("default_cves") or ()
    overrides = dataset.get("per_cell_overrides") or {}
    cell_override = overrides.get(cell) or {}
    subset = cell_override.get("subset")

    effective = list(subset) if subset is not None else list(defaults)
    if task not in effective:
        raise ValueError(
            f"task {task!r} is not in the effective CVE set for cell {cell!r}: {effective}"
        )


def register_run(
    *,
    study_id: str,
    run_id: UUID,
    cell: str,
    task: str,
    replicate: int | None = None,
    attempt: int | None = None,
    output_directory: str | Path | None = None,
    projection_status: str = "ok",
) -> None:
    """Stamp experiment-level fields onto ``run_id``'s run_manifest.json.

    Validates (cell declared, task in cell scope) against
    ``experiments/<study>/manifest.yaml`` but never writes back to it — the
    enrollment roster is a derived artifact regenerated by
    ``experiments.shared.scripts.collect``.

    ``replicate`` is the canonical name; ``attempt`` is accepted as an alias
    for one rename cycle. Exactly one of them must be provided.
    ``projection_status`` defaults to ``"ok"``; the harness passes
    ``"failed"`` when event projection raised so downstream rollups can
    exclude the row (audit N-2).
    """
    if replicate is not None and attempt is not None and replicate != attempt:
        raise ValueError(
            f"register_run got conflicting replicate={replicate} and attempt={attempt}"
        )
    if replicate is not None:
        effective_replicate = replicate
    elif attempt is not None:
        effective_replicate = attempt
    else:
        raise ValueError("register_run requires `replicate` (or its alias `attempt`)")

    study_manifest_path = _study_manifest_path(study_id)
    study = _load_yaml(study_manifest_path)
    study.setdefault("study_id", study_id)
    _validate_cell_declared(study, cell)
    _validate_task_in_cell_scope(study, cell, task)

    runs_root = _runs_root(output_directory)
    run_manifest_path = _run_manifest_path(run_id, runs_root)

    run_manifest_payload = _prepare_run_manifest_payload(
        run_manifest_path,
        study_id=study_id,
        cell=cell,
        task=task,
        replicate=effective_replicate,
        projection_status=projection_status,
    )
    _write_run_manifest(run_manifest_path, run_manifest_payload)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="register_run",
        description="Stamp experiment-level fields onto a run's run_manifest.json.",
    )
    parser.add_argument("--study", required=True, help="Study id, e.g. 2026-04-22-demo")
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--cell", required=True, help="Cell slug (A1, B2, ...)")
    parser.add_argument("--task", required=True, help="Task name (CVE slug, etc.)")
    replicate_group = parser.add_mutually_exclusive_group(required=True)
    replicate_group.add_argument("--replicate", type=int, help="Sample index within (cell, task)")
    replicate_group.add_argument(
        "--attempt",
        type=int,
        help="Deprecated alias for --replicate (kept for one rename cycle)",
    )
    parser.add_argument(
        "--output-directory",
        help="Override the runs/ root (default: <repo_root>/runs)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    try:
        register_run(
            study_id=args.study,
            run_id=args.run_id,
            cell=args.cell,
            task=args.task,
            replicate=args.replicate,
            attempt=args.attempt,
            output_directory=args.output_directory,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"register_run: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
