"""Enroll an existing run into a study manifest.

The runtime writes runtime-level fields (`started_at`, `models`, `tokens`, …)
into `runs/<run_id>/run_manifest.json` when `main.py run` executes; the
experiment-level fields (`cell`, `task`, `attempt`, `study_id`) are merged
in here by the harness after the subprocess returns (spec §10.3), or by a
human invoking this script for a historical run.

Usage::

    python -m experiments.shared.scripts.register_run \
        --study 2026-04-22-naive-vs-cybersec \
        --run-id <uuid> \
        --cell B2 \
        --task gpac.cve-2021-40575 \
        --attempt 0
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


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _upsert_run_in_study(
    study: dict[str, Any],
    *,
    run_id: UUID,
    cell: str,
    task: str,
    attempt: int,
) -> None:
    """Add or update the row for `run_id` in study.runs. Idempotent."""
    runs: list[dict[str, Any]] = list(study.get("runs") or [])
    for index, existing in enumerate(runs):
        if str(existing.get("run_id")) == str(run_id):
            runs[index] = {
                "run_id": str(run_id),
                "cell": cell,
                "task": task,
                "attempt": attempt,
            }
            study["runs"] = runs
            return
    runs.append(
        {
            "run_id": str(run_id),
            "cell": cell,
            "task": task,
            "attempt": attempt,
        }
    )
    study["runs"] = runs


def _prepare_run_manifest_payload(
    run_manifest_path: Path,
    *,
    study_id: str,
    cell: str,
    task: str,
    attempt: int,
) -> dict[str, Any]:
    """Load and merge experiment-level fields into the run manifest in memory.

    Does not write anything. Raises FileNotFoundError if the run manifest is
    absent so callers can fail fast before mutating the study manifest.
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
            "attempt": attempt,
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
        raise ValueError(
            f"cell {cell!r} not declared in study.cells (declared: {declared})"
        )


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
            f"study declares dataset={dataset_rel!r} but the file was not found "
            f"at {dataset_path}"
        )

    dataset = yaml.safe_load(dataset_path.read_text(encoding="utf-8")) or {}
    defaults: Iterable[str] = dataset.get("default_cves") or ()
    overrides = dataset.get("per_cell_overrides") or {}
    cell_override = overrides.get(cell) or {}
    subset = cell_override.get("subset")

    effective = list(subset) if subset is not None else list(defaults)
    if task not in effective:
        raise ValueError(
            f"task {task!r} is not in the effective CVE set for cell {cell!r}: "
            f"{effective}"
        )


def register_run(
    *,
    study_id: str,
    run_id: UUID,
    cell: str,
    task: str,
    attempt: int,
    output_directory: str | Path | None = None,
) -> None:
    """Enroll ``run_id`` into study ``study_id`` for the given cell/task/attempt.

    Transactional ordering (spec §8, must-fix #4):

    1. Validate (cell declared, task in cell scope).
    2. Build the updated study manifest and run_manifest payloads in memory.
       Fails fast if the run_manifest doesn't exist — nothing is written yet.
    3. Write the study manifest FIRST. After this step the run is enrolled.
    4. Write the run_manifest SECOND. The run_manifest only caches the
       experiment-level fields already durable in the study manifest, so a
       crash between 3 and 4 is recoverable: re-running ``register_run`` with
       the same arguments completes the back-fill. The operator is notified
       via a logged error.

    Idempotent: re-running with the same ``run_id`` overwrites the study-row
    in place (``_upsert_run_in_study``) and rewrites the run_manifest with
    the same merged payload (``_write_run_manifest``).
    """
    study_manifest_path = _study_manifest_path(study_id)
    study = _load_yaml(study_manifest_path)
    study.setdefault("study_id", study_id)
    _validate_cell_declared(study, cell)
    _validate_task_in_cell_scope(study, cell, task)

    runs_root = _runs_root(output_directory)
    run_manifest_path = _run_manifest_path(run_id, runs_root)

    # Build both payloads in memory before touching disk so a validation
    # failure (e.g. missing run_manifest) aborts cleanly.
    run_manifest_payload = _prepare_run_manifest_payload(
        run_manifest_path,
        study_id=study_id,
        cell=cell,
        task=task,
        attempt=attempt,
    )
    _upsert_run_in_study(study, run_id=run_id, cell=cell, task=task, attempt=attempt)

    # Study manifest first: this is the source of truth for enrollment.
    _dump_yaml(study_manifest_path, study)

    # Run manifest second: a failure here leaves the run enrolled but without
    # cached experiment-level fields. Re-running register_run with the same
    # arguments backfills cleanly.
    try:
        _write_run_manifest(run_manifest_path, run_manifest_payload)
    except OSError:
        logger.exception(
            "enrolled run_id=%s for study=%s but failed to update "
            "run_manifest.json at %s; re-run register_run to backfill",
            run_id,
            study_id,
            run_manifest_path,
        )
        raise


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="register_run",
        description="Enroll an existing run into a study manifest.",
    )
    parser.add_argument("--study", required=True, help="Study id, e.g. 2026-04-22-demo")
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--cell", required=True, help="Cell slug (A1, B2, …)")
    parser.add_argument("--task", required=True, help="Task name (CVE slug, etc.)")
    parser.add_argument("--attempt", type=int, required=True)
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
            attempt=args.attempt,
            output_directory=args.output_directory,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"register_run: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
