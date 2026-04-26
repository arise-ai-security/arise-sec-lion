"""Top-level study harness: `run-ours` and `run-baseline` subcommands.

Depends only on stdlib + subprocess + yaml plus the cross-study utilities
in ``experiments/shared/scripts``. No import of core/, plugins/, or
infrastructure/ — the harness is pure orchestration around ``main.py run``
and baseline subprocesses (spec §8).

Run the A/B cells sequentially by default: the harness relies on
``runs/.last_run.json`` to learn the boss_id emitted by ``main.py run``,
and that file is inherently a single-slot pointer (spec §12).

Usage::

    python -m experiments.shared.harness run-ours \\
        --study 2026-04-22-demo \\
        --cell B2 \\
        --task gpac.cve-2021-40575 \\
        --attempt 0 \\
        --config experiments/2026-04-22-demo/configs/B2.yaml

    python -m experiments.shared.harness run-baseline \\
        --study 2026-04-22-demo \\
        --cell A1 \\
        --task gpac.cve-2021-40575 \\
        --attempt 0 \\
        --variant claude-code-subagent
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import yaml

from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.project_events import project_events_to_jsonl
from experiments.shared.scripts.register_run import register_run


if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)


_BASELINE_MODULES: dict[str, str] = {
    "claude-code-subagent": "experiments.shared.baselines.run_claude_code",
    "claude-code-nosubagent": "experiments.shared.baselines.run_claude_code_no_subagent",
}


# =============================================================================
# Manifest-driven dispatch (PR 2 onward)
# =============================================================================
# `run_ours` and `run_baseline` below are still the load-bearing implementations
# of the two execution paths today. Starting in PR 2, they are dispatched via
# the runner registry (`experiments/shared/runners/aris.py`) rather than read
# from a free-form `harness:` field in the study manifest. PR 6's
# `run_matrix.py` resolves a cell to a runner via `runners.get(cell.runner)`
# and calls `runner.run(...)`, which in turn calls back into these helpers.
# PR 4b will introduce flat-mode dispatch in `aris.run` that may bypass
# `run_baseline` entirely once flat configs land. Until then, A-cells flow
# through `run_baseline` and B-cells flow through `run_ours` exactly as before.

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


def _ensure_task_coverage(dataset: dict[str, Any]) -> dict[str, Path]:
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
                f"dataset.source.paths has no entry matching task {task!r}; "
                f"found paths: {paths}"
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
        stem = Path(path).stem.lower()
        if normalized == stem:
            return path

    # Second pass: substring fallback, only if no exact match exists.
    for path in paths:
        stem = Path(path).stem.lower()
        if normalized in stem:
            return path
    return None


def _resolve_effective_cves(
    dataset: dict[str, Any], cell: str
) -> list[str]:
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
        raise ValueError(
            f"cell {cell!r} is not declared in study.cells (declared: {declared})"
        )


# =============================================================================
# `run-ours` path
# =============================================================================


def _read_last_run_id(runs_root: Path) -> UUID:
    pointer = runs_root / ".last_run.json"
    if not pointer.is_file():
        raise FileNotFoundError(
            f"missing {pointer}; `main.py run` must have failed before writing it"
        )
    data = json.loads(pointer.read_text(encoding="utf-8"))
    try:
        return UUID(data["boss_id"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f".last_run.json is missing or has an invalid boss_id: {data}") from exc


def _last_run_snapshot(runs_root: Path) -> tuple[str | None, float | None]:
    """Return (boss_id, mtime) for the current `.last_run.json`, if present.

    Used as a before/after check so we never enroll a stale pointer when
    `main.py run` dies before it can update the file (spec §8 + codex
    review Phase 3 P1).
    """
    pointer = runs_root / ".last_run.json"
    if not pointer.is_file():
        return None, None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
        boss_id = data.get("boss_id")
    except (OSError, json.JSONDecodeError):
        return None, None
    try:
        mtime = pointer.stat().st_mtime
    except OSError:
        mtime = None
    return boss_id, mtime


def _invoke_main_py(
    *,
    config: Path,
    task: str,
    context_file: Path,
    python_bin: str | None = None,
) -> int:
    """Run ``python main.py -c <config> run <task> --domain security --domain-context-file <path>``.

    `-c` MUST come BEFORE the `run` subcommand (top-level flag, see
    bootstrap/bootstrap.py:67). Returns the subprocess exit code.
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
    logger.info("invoking: %s", " ".join(cmd))
    completed = subprocess.run(cmd, check=False, cwd=get_repo_root())  # noqa: S603
    return completed.returncode


def run_ours(
    *,
    study_id: str,
    cell: str,
    task: str,
    attempt: int,
    config: Path,
    runs_root: Path | None = None,
) -> UUID:
    """Execute an A/B run through `main.py run` and enroll it in the study."""
    manifest = _load_study_manifest(study_id)
    _validate_cell_declared(manifest, cell)
    dataset = _load_dataset(study_id, manifest=manifest)

    coverage = _ensure_task_coverage(dataset)
    effective = _resolve_effective_cves(dataset, cell)
    if task not in effective:
        raise ValueError(
            f"task {task!r} is not in the effective CVE set for cell {cell!r}: {effective}"
        )

    context_file = coverage[task]
    pool = runs_root or _runs_root()
    # Capture the pointer state BEFORE the subprocess so we can tell the
    # difference between "this run advanced it" vs. "we're about to enroll a
    # stale pointer from a previous run" (codex review Phase 3 P1).
    prev_boss_id, prev_mtime = _last_run_snapshot(pool)

    exit_code = _invoke_main_py(config=config, task=task, context_file=context_file)
    # `main.py run` exits non-zero on failed/timeout runs, but the run manifest
    # still carries the outcome. We do NOT bail out here — enroll the run so
    # its exit_status is discoverable. A raise happens only if .last_run.json
    # wasn't advanced by this subprocess (meaning the run died before any
    # artifacts landed on disk).
    if exit_code != 0:
        logger.warning("`main.py run` exited with %d; continuing enrollment", exit_code)

    curr_boss_id, curr_mtime = _last_run_snapshot(pool)
    if curr_boss_id is None:
        raise FileNotFoundError(
            "`main.py run` produced no `.last_run.json`; refusing to enroll"
        )
    pointer_advanced = (curr_boss_id != prev_boss_id) or (
        prev_mtime is not None and curr_mtime is not None and curr_mtime > prev_mtime
    )
    if not pointer_advanced:
        raise RuntimeError(
            "`.last_run.json` was not advanced by the subprocess (boss_id="
            f"{curr_boss_id!r}); refusing to enroll a stale pointer."
        )
    run_id = UUID(curr_boss_id)

    # Write events.jsonl + stamp the per-run manifest with cell/task/replicate.
    # Pass the pinned config so the projection queries the same Postgres
    # instance `main.py run` just wrote to. The study manifest is design-only
    # (PR 1): the enrollment roster is regenerated by collect.py.
    events_path = pool / str(run_id) / "events.jsonl"
    try:
        asyncio.run(
            project_events_to_jsonl(
                run_id=run_id,
                output_path=events_path,
                config_path=config,
            )
        )
    except Exception:
        logger.exception(
            "event projection failed for run_id=%s; continuing with enrollment",
            run_id,
        )

    # `attempt=` is the deprecated alias for `replicate=`; both call sites in
    # this module retain it for one PR cycle. PR 6 (Task 7 in the experiments
    # rearchitecture plan) drops the alias and switches to `replicate=`.
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell=cell,
        task=task,
        attempt=attempt,
        output_directory=pool,
    )
    return run_id


# =============================================================================
# `run-baseline` path
# =============================================================================


def _pick_baseline_runner(variant: str) -> Callable[..., dict[str, object]]:
    module_name = _BASELINE_MODULES.get(variant)
    if module_name is None:
        raise ValueError(
            f"unknown baseline variant {variant!r}; "
            f"known variants: {sorted(_BASELINE_MODULES)}"
        )
    module = importlib.import_module(module_name)
    if not hasattr(module, "run_baseline"):
        raise RuntimeError(f"baseline module {module_name} has no run_baseline()")
    return module.run_baseline


def _current_git_sha() -> str | None:
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git_bin, "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout.strip() or None


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_baseline(
    *,
    study_id: str,
    cell: str,
    task: str,
    attempt: int,
    variant: str,
    runs_root: Path | None = None,
) -> UUID:
    """Mint a run_id, run the baseline, write its manifest, and enroll.

    KNOWN LIMITATION: this path does NOT yet materialize the SEC-bench
    workspace (`/src`, `/testcase`, `secb`) that `main.py run` prepares via
    SecurityDomainPlugin. Baselines therefore start from an empty run dir and
    must rely on the shared briefing + context JSON to locate the target
    source. A follow-up should expose workspace prep as a standalone CLI that
    the harness can shell out to before dispatching to the baseline runner.
    """
    runner = _pick_baseline_runner(variant)

    manifest = _load_study_manifest(study_id)
    _validate_cell_declared(manifest, cell)
    dataset = _load_dataset(study_id, manifest=manifest)
    coverage = _ensure_task_coverage(dataset)

    effective = _resolve_effective_cves(dataset, cell)
    if task not in effective:
        raise ValueError(
            f"task {task!r} is not in the effective CVE set for cell {cell!r}: {effective}"
        )

    context_file = coverage[task]
    pool = runs_root or _runs_root()
    run_id, run_dir = _mint_fresh_run_dir(pool)

    started = datetime.now(UTC)
    extras = runner(
        run_dir=run_dir,
        task_prompt=task,
        context_file=context_file,
    )
    ended = datetime.now(UTC)

    baseline_payload: dict[str, Any] = {
        "run_id": str(run_id),
        "kind": "claude_code_baseline",
        "started_at": _iso_utc(started),
        "ended_at": _iso_utc(ended),
        "exit_status": extras.get("exit_status", "failed"),
        "variant": extras.get("variant", variant),
        "git_sha": _current_git_sha(),
    }
    manifest_path = run_dir / "run_manifest.json"
    tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp.write_text(json.dumps(baseline_payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(manifest_path)

    # See `run_ours` above: `attempt=` is the deprecated alias retained for
    # one PR cycle (PR 6 / Task 7 drops it).
    register_run(
        study_id=study_id,
        run_id=run_id,
        cell=cell,
        task=task,
        attempt=attempt,
        output_directory=pool,
    )
    return run_id


def _runs_root() -> Path:
    return get_repo_root() / "runs"


_MAX_RUN_ID_RETRIES = 3


def _mint_fresh_run_dir(pool: Path) -> tuple[UUID, Path]:
    """Mint a uuid4 and create ``pool/<uuid>/`` exclusively.

    uuid4 collisions are astronomically unlikely, but ``exist_ok=True`` would
    silently overwrite an existing run directory. Retry up to
    ``_MAX_RUN_ID_RETRIES`` times on FileExistsError; other OSErrors
    propagate so disk/permission failures are not masked.
    """
    for _attempt in range(_MAX_RUN_ID_RETRIES):
        candidate = uuid4()
        run_dir = pool / str(candidate)
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return candidate, run_dir
    raise RuntimeError(
        f"failed to mint a unique run_id after {_MAX_RUN_ID_RETRIES} attempts"
    )


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
    ours.add_argument("--attempt", type=int, required=True)
    ours.add_argument("--config", type=Path, required=True)

    baseline = sub.add_parser("run-baseline", help="Run a baseline tool and enroll it")
    baseline.add_argument("--study", required=True)
    baseline.add_argument("--cell", required=True)
    baseline.add_argument("--task", required=True)
    baseline.add_argument("--attempt", type=int, required=True)
    baseline.add_argument(
        "--variant",
        required=True,
        choices=sorted(_BASELINE_MODULES),
    )

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
                attempt=args.attempt,
                config=args.config,
            )
        elif args.command == "run-baseline":
            run_baseline(
                study_id=args.study,
                cell=args.cell,
                task=args.task,
                attempt=args.attempt,
                variant=args.variant,
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
