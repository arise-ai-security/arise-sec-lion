"""Experiment runner -- orchestrates Pillar A's (CVE x cell x replicate) schedule.

Reads the locked_instances.yaml produced by Task 15, constructs the full schedule,
dispatches each run to either the flat CLI harness (cells A1-A4) or the tree
(cells B1-B2 via `python main.py`), invokes the mechanical evaluator and the
anti-cheat audit after each run, and appends one line per completed run to
INDEX.jsonl. Resume-safe: runs whose ``events.jsonl`` already exists are skipped.

Sanitizer-error normalization
-----------------------------

The mechanical evaluator requires a BARE sanitizer-class string (see
:data:`experiments.mechanical_evaluator.SANITIZER_ERROR_CLASSES`), while
:class:`plugins.security.cve_instance.CVEInstance` returns FRAMED strings
like ``"ERROR: AddressSanitizer: heap-buffer-overflow"``.
:func:`_normalize_sanitizer_error` bridges the two so runs fail loudly
instead of silently misclassifying.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml

from experiments.audit_cheating import audit_run
from experiments.mechanical_evaluator import SANITIZER_ERROR_CLASSES, evaluate_run
from experiments.schema import IndexEntry


logger = logging.getLogger(__name__)

CELLS_ALL: tuple[str, ...] = ("A1", "A2", "A3", "A4", "B1", "B2")
TREE_CELLS: frozenset[str] = frozenset({"B1", "B2"})


@dataclass(frozen=True)
class RunPlan:
    """One scheduled experiment run (cve_id, cell, replicate, run_dir)."""

    cve_id: str
    cell: str
    replicate: int
    run_dir: Path


def plan_runs(
    *,
    cves: list[str],
    cells: list[str],
    anchor_cve: str,
    anchor_cell: str,
    anchor_replicates: int,
    seed: int,
    output_root: Path = Path("dataset/runs"),
) -> list[RunPlan]:
    """Construct the full schedule of runs.

    Cell ordering is shuffled per CVE using a seeded RNG so the order is
    reproducible but not globally identical across CVEs -- this spreads any
    time-of-day effects across cells.

    In addition to the Cartesian product, the anchor (cve, cell) pair receives
    ``anchor_replicates`` total runs (replicate indices 0, 1, ..., N-1). The
    replicate-0 entry is already produced by the Cartesian product, so only
    the extra replicates 1..N-1 are appended here.
    """
    # Seeded RNG is used strictly for deterministic cell-ordering; no crypto.
    rng = random.Random(seed)  # noqa: S311
    plans: list[RunPlan] = []
    for cve_id in cves:
        order = list(cells)
        rng.shuffle(order)
        for cell in order:
            plans.append(
                RunPlan(
                    cve_id=cve_id,
                    cell=cell,
                    replicate=0,
                    run_dir=output_root / cve_id / cell / "0",
                )
            )
    # Extra replicates on the anchor (beyond the base 0th already scheduled).
    for r in range(1, anchor_replicates):
        plans.append(
            RunPlan(
                cve_id=anchor_cve,
                cell=anchor_cell,
                replicate=r,
                run_dir=output_root / anchor_cve / anchor_cell / str(r),
            )
        )
    return plans


def is_resumable(plan: RunPlan) -> bool:
    """Return True if this run already produced an events.jsonl (skip on resume)."""
    return (plan.run_dir / "events.jsonl").exists()


def _normalize_sanitizer_error(framed_or_bare: str) -> str:
    """Extract a bare sanitizer-class string from a CVEInstance framed string.

    Raises ValueError if no known class (per :data:`SANITIZER_ERROR_CLASSES`)
    is present -- the mechanical evaluator requires a bare class and a silent
    substring match would corrupt the primary metric.
    """
    text_lower = framed_or_bare.lower()
    for cls in SANITIZER_ERROR_CLASSES:
        if cls.lower() in text_lower:
            return cls
    raise ValueError(
        f"Could not extract a known sanitizer class from {framed_or_bare!r}. "
        f"Known classes: {SANITIZER_ERROR_CLASSES}"
    )


async def dispatch_run(
    plan: RunPlan,
    cve_json_path: Path,
    task_text: str,
    docker_image: str,
) -> dict[str, Any]:
    """Send the plan to the correct execution path; capture outcome dict."""
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    if plan.cell in TREE_CELLS:
        return await _dispatch_tree(plan, cve_json_path, task_text)
    return await _dispatch_flat(plan, cve_json_path, task_text, docker_image)


async def _dispatch_flat(
    plan: RunPlan,
    cve_json_path: Path,
    task_text: str,
    docker_image: str,
) -> dict[str, Any]:
    """Run a flat-CLI cell (A1-A4) via :func:`run_flat_cli`."""
    from experiments.baselines.flat_cli_harness import (
        CELL_CONFIGS,
        RunSpec,
        run_flat_cli,
    )

    cell_cfg = CELL_CONFIGS[plan.cell]
    workspace_root = plan.run_dir / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    spec = RunSpec(
        cve_instance_path=cve_json_path,
        cell=cell_cfg,
        replicate=plan.replicate,
        output_dir=plan.run_dir,
        docker_image=docker_image,
        workspace_host_root=workspace_root,
    )
    meta = await run_flat_cli(spec, task_text)
    return {"system": "flat_cli", "meta": meta}


async def _dispatch_tree(
    plan: RunPlan,
    cve_json_path: Path,
    task_text: str,
) -> dict[str, Any]:
    """Invoke ``python main.py run`` with the per-cell config.

    KNOWN GAP (Task 16 concern): the current ``main.py run`` CLI accepts
    ``-c/--config`` and ``--domain-context-file`` but NOT ``--cve-file`` or
    ``--output-dir``. Adding those flags to bootstrap is out of scope for
    Task 13. Until Task 16 wires them up, this dispatch:

    * passes the per-cell config via ``-c`` (verified present),
    * passes the CVE JSON via ``--domain-context-file`` (verified present),
    * does NOT pass ``--output-dir`` -- the tree writes to its default
      output directory; ``experiments/tree_projection.py`` (stub) is the
      hook that will later copy the run into ``plan.run_dir``.

    The tree-projection stub currently logs a warning and exits 0; no
    ``events.jsonl`` is produced for tree runs at this stage.
    """
    config_path = Path(f"config/exp-secbench-{plan.cell}.yaml")
    cmd = [
        sys.executable,
        "main.py",
        "-c",
        str(config_path),
        "run",
        task_text,
        "--domain",
        "security",
        "--domain-context-file",
        str(cve_json_path),
    ]
    started = time.monotonic()
    log_path = plan.run_dir / "stdout_stderr.log"
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_f:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_f,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
    duration = time.monotonic() - started

    # Project the event store into events.jsonl + meta.json (currently a stub).
    projection_cmd = [
        sys.executable,
        "-m",
        "experiments.tree_projection",
        "--run-dir",
        str(plan.run_dir),
    ]
    projection_proc = await asyncio.create_subprocess_exec(
        *projection_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await projection_proc.wait()

    meta_path = plan.run_dir / "meta.json"
    meta_data: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Could not parse meta.json for %s", plan.run_dir)
    return {"system": "tree", "meta": meta_data, "duration": duration}


async def execute_all(
    plans: list[RunPlan],
    instances: dict[str, dict[str, Any]],
    *,
    index_path: Path | None = None,
) -> None:
    """Run every plan, skipping complete ones; append each run summary to INDEX.jsonl.

    A failure in one run (dispatch, audit, mechanical, or index) is logged
    and the loop continues with the next plan -- the whole 63-run pipeline
    never stops because of a single bad run.
    """
    index_path = index_path or Path("dataset/INDEX.jsonl")
    index_path.parent.mkdir(parents=True, exist_ok=True)

    for plan in plans:
        if is_resumable(plan):
            logger.info("Skipping completed run: %s", plan.run_dir)
            continue

        cve_data = instances[plan.cve_id]
        cve_json_path = Path(cve_data["json_path"])
        task_text = str(cve_data["task_text"])
        docker_image = str(cve_data["docker_image"])

        logger.info("Starting run: %s", plan.run_dir)
        try:
            await dispatch_run(plan, cve_json_path, task_text, docker_image)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Run failed: %s", plan.run_dir)
            continue

        audit_report: dict[str, Any]
        mech: dict[str, Any]
        try:
            audit_report = audit_run(
                plan.run_dir / "events.jsonl",
                base_commit=str(cve_data["base_commit"]),
            )
            (plan.run_dir / "audit.json").write_text(
                json.dumps(audit_report, indent=2), encoding="utf-8"
            )
            sanitizer_class = _normalize_sanitizer_error(str(cve_data["expected_sanitizer_error"]))
            mech = evaluate_run(
                workspace=plan.run_dir / "workspace",
                docker_image=docker_image,
                expected_sanitizer_error=sanitizer_class,
            )
            (plan.run_dir / "mechanical.json").write_text(
                json.dumps(mech, indent=2), encoding="utf-8"
            )
        except (OSError, ValueError):
            # FileNotFoundError is a subclass of OSError and is covered here.
            logger.exception("Post-run analysis failed: %s", plan.run_dir)
            continue

        try:
            entry = IndexEntry(
                run_id=f"{plan.cve_id}-{plan.cell}-{plan.replicate}",
                cve_id=plan.cve_id,
                cell=plan.cell,
                replicate=plan.replicate,
                path=str(plan.run_dir),
                termination_reason=_read_termination(plan),
                wallclock_seconds=_read_wallclock(plan),
                total_cost_usd=_sum_cost(plan),
                event_count=_count_events(plan),
                artifacts_produced=_list_artifacts(plan),
                mechanical_pass={
                    "builder": bool(mech["builder_pass"]),
                    "exploiter": bool(mech["exploiter_pass"]),
                    "fixer": bool(mech["fixer_pass"]),
                    "end_to_end": bool(mech["end_to_end_pass"]),
                },
                audit_violations=int(audit_report["violation_count"]),
            )
            with index_path.open("a", encoding="utf-8") as fh:
                fh.write(entry.model_dump_json() + "\n")
        except (OSError, ValueError):
            logger.exception("Failed to write index entry for: %s", plan.run_dir)


def _read_termination(plan: RunPlan) -> str:
    meta_path = plan.run_dir / "meta.json"
    if not meta_path.exists():
        return "unknown"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unknown"
    return str(data.get("termination_reason", "unknown"))


def _read_wallclock(plan: RunPlan) -> float:
    meta_path = plan.run_dir / "meta.json"
    if not meta_path.exists():
        return 0.0
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0.0
    return float(data.get("wallclock_seconds", 0.0))


def _sum_cost(plan: RunPlan) -> float:
    events_path = plan.run_dir / "events.jsonl"
    if not events_path.exists():
        return 0.0
    total = 0.0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event_type") == "tokens_consumed":
            payload = ev.get("payload") or {}
            cost = payload.get("cost_usd")
            if cost is not None:
                total += float(cost)
    return total


def _count_events(plan: RunPlan) -> int:
    events_path = plan.run_dir / "events.jsonl"
    if not events_path.exists():
        return 0
    return sum(1 for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip())


def _list_artifacts(plan: RunPlan) -> list[str]:
    art_dir = plan.run_dir / "artifacts"
    if not art_dir.exists():
        return []
    return sorted(p.name for p in art_dir.iterdir())


@click.command()
@click.option(
    "--locked-instances",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to the locked-instances YAML (Task 15).",
)
@click.option(
    "--cells",
    default="all",
    help="Comma-separated cells (e.g. 'A1,A2') or 'all' for every cell.",
)
@click.option(
    "--output-dir",
    default=Path("dataset/runs"),
    type=click.Path(path_type=Path),
    help="Where per-run subdirectories are written.",
)
@click.option("--seed", default=42, type=int, help="RNG seed for cell-order shuffling.")
def main(locked_instances: Path, cells: str, output_dir: Path, seed: int) -> None:
    """Run the experiment over the (CVE x cell x replicate) Cartesian schedule."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    data = yaml.safe_load(locked_instances.read_text(encoding="utf-8"))
    cve_ids: list[str] = [inst["instance_id"] for inst in data["instances"]]
    cell_list = list(CELLS_ALL) if cells == "all" else cells.split(",")
    plans = plan_runs(
        cves=cve_ids,
        cells=cell_list,
        anchor_cve=data["anchor"]["cve_id"],
        anchor_cell=data["anchor"]["cell"],
        anchor_replicates=int(data["anchor"]["replicates"]),
        seed=seed,
        output_root=output_dir,
    )
    instances = {inst["instance_id"]: inst for inst in data["instances"]}
    asyncio.run(execute_all(plans, instances))


if __name__ == "__main__":
    main()
