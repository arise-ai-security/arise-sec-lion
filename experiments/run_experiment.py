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
import fcntl
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
from plugins.security import CVEInstance, resolve_secbench_image


DEFAULT_CONCURRENCY = 5
"""Default number of parallel (CVE, cell, replicate) runs.

Tuning guide:
- Anthropic API TPM caps are the dominant constraint. N=5 assumes Tier 2+
  (>=2M TPM). On Tier 1 (~400K-1M TPM), drop to N=2 or risk 429s.
- Host RAM: each Docker workload peaks at 2-4 GB. N=5 -> 10-20 GB + Python
  overhead; comfortable on >=32 GB hosts.
- Docker daemon throughput handles 5 concurrent containers easily.
- Postgres arise-db is fine with 5 concurrent writers (each BOSS has a
  unique aggregate_id, so no write hot-spot).
"""


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

    ``output_root`` SHOULD be absolute; :func:`main` ensures this before
    calling (a relative path propagates into Docker ``-v`` args and is
    silently interpreted as a named volume, mounting an empty dir).
    """
    if anchor_replicates > 1:
        if anchor_cve not in cves:
            raise ValueError(
                f"anchor_cve={anchor_cve!r} must be in cves; otherwise the base "
                f"(replicate=0) anchor run is missing while extras are scheduled."
            )
        if anchor_cell not in cells:
            raise ValueError(
                f"anchor_cell={anchor_cell!r} must be in cells={cells!r} for the "
                f"same reason."
            )
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
    """Return True if this run already wrote mechanical.json (fully complete)."""
    return (plan.run_dir / "mechanical.json").exists()


def _normalize_sanitizer_error(framed_or_bare: str) -> str:
    """Extract a bare sanitizer-class string from a CVEInstance framed string.

    Raises ValueError if no known class (per :data:`SANITIZER_ERROR_CLASSES`)
    is present -- the mechanical evaluator requires a bare class and a silent
    substring match would corrupt the primary metric.

    Known failure shapes (these raise ValueError and drop the CVE from the
    primary metric until :class:`plugins.security.cve_instance.CVEInstance`
    is fixed upstream):

    * ``"ERROR: MemorySanitizer"`` -- no class suffix after the sanitizer name.
    * ``"runtime error"`` -- UBSan-style string with no class.
    * ``"ERROR: <Sanitizer>Sanitizer"`` fallback -- no bare class present.
    """
    text_lower = framed_or_bare.lower()
    for cls in SANITIZER_ERROR_CLASSES:
        if cls.lower() in text_lower:
            return cls
    raise ValueError(
        f"Could not extract a known sanitizer class from {framed_or_bare!r}. "
        f"Known classes: {SANITIZER_ERROR_CLASSES}"
    )


def _validate_cve_data(cve_data: dict[str, Any], cve_id: str) -> None:
    """Raise ValueError early if required keys are missing from a locked-instance entry."""
    required = (
        "json_path",
        "task_text",
        "docker_image",
        "base_commit",
        "expected_sanitizer_error",
    )
    missing = [k for k in required if k not in cve_data]
    if missing:
        raise ValueError(
            f"locked_instances entry for {cve_id!r} missing required keys: {missing}"
        )


def _preflight_check_sanitizer_normalization(instances: list[dict[str, Any]]) -> None:
    """Warn (don't fail) for CVEs whose expected_sanitizer_error cannot be normalized.

    Such CVEs will not produce a mechanical.json and will be missing from
    INDEX.jsonl. The operator may have intentionally included MSan/UBSan CVEs
    that cannot be mechanically evaluated in v1.0, so we warn and continue
    rather than abort.
    """
    for inst in instances:
        try:
            _normalize_sanitizer_error(str(inst["expected_sanitizer_error"]))
        except ValueError as exc:
            logger.warning(
                "CVE %r will not be mechanically evaluated: %s. "
                "TODO(task-15): include only CVEs with extractable bare classes, "
                "OR fix CVEInstance.expected_sanitizer_error to include the class "
                "for MSan/UBSan.",
                inst["instance_id"],
                exc,
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
        "--output-dir",
        str(plan.run_dir),
    ]
    started = time.monotonic()
    log_path = plan.run_dir / "stdout_stderr.log"
    plan.run_dir.mkdir(parents=True, exist_ok=True)
    (plan.run_dir / "workspace").mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_f:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=log_f,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
    if proc.returncode != 0:
        logger.warning(
            "Tree run exited non-zero (%d) for %s",
            proc.returncode,
            plan.run_dir,
        )
    duration = time.monotonic() - started

    # Project the event store into events.jsonl + meta.json (currently a stub).
    # stdout/stderr go to DEVNULL to avoid the classic PIPE + await deadlock
    # (64KB pipe buffer fills once the real projection ships richer output);
    # any diagnostics should be written to files under plan.run_dir, not stdout.
    projection_cmd = [
        sys.executable,
        "-m",
        "experiments.tree_projection",
        "--run-dir",
        str(plan.run_dir),
    ]
    projection_proc = await asyncio.create_subprocess_exec(
        *projection_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await projection_proc.wait()
    if projection_proc.returncode != 0:
        logger.warning(
            "tree_projection exited non-zero (%d) for %s",
            projection_proc.returncode,
            plan.run_dir,
        )

    meta_path = plan.run_dir / "meta.json"
    meta_data: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Could not parse meta.json for %s", plan.run_dir)
    return {"system": "tree", "meta": meta_data, "duration": duration}


def _append_index_entry_locked(index_path: Path, entry: IndexEntry) -> None:
    """Append one JSONL line to INDEX.jsonl with an exclusive advisory lock.

    Multiple concurrent runs call this to record their outcome. POSIX
    ``fcntl.LOCK_EX`` serializes writers across processes on the same
    filesystem, preventing interleaved bytes when two runs finish within
    the same instant. The lock is released automatically when the file
    handle is closed.
    """
    with index_path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(entry.model_dump_json() + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


async def _run_one(
    plan: RunPlan,
    instances: dict[str, dict[str, Any]],
    index_path: Path,
    semaphore: asyncio.Semaphore,
) -> None:
    """Execute a single plan under the concurrency semaphore.

    Semaphore caps how many runs are in-flight simultaneously. Any failure
    is logged but never re-raised: per-run isolation is the explicit design
    goal so one bad CVE (malformed YAML, docker hiccup) can't stall the
    rest of the schedule.
    """
    async with semaphore:
        if is_resumable(plan):
            logger.info("Skipping completed run: %s", plan.run_dir)
            return

        cve_data = instances[plan.cve_id]
        cve_json_path = Path(cve_data["json_path"])
        task_text = str(cve_data["task_text"])
        # Derive the tools-enriched image from the CVE JSON (authoritative
        # source of :patch tag). The flat-CLI harness invokes ``claude`` and
        # ``secb`` inside the container, which requires the tools layer added
        # by deployment/secbench-tools.Dockerfile. The mechanical evaluator
        # only needs ``secb``, but uses the same image for consistency.
        try:
            _cve_inst = CVEInstance.from_json_file(cve_json_path)
            docker_image = resolve_secbench_image(
                _cve_inst.docker_image, security_tools_enabled=True
            )
        except Exception:
            logger.exception("Pre-run setup failed: %s", plan.run_dir)
            return

        logger.info("Starting run: %s (image: %s)", plan.run_dir, docker_image)
        try:
            await dispatch_run(plan, cve_json_path, task_text, docker_image)
        except Exception:
            logger.exception("Run failed: %s", plan.run_dir)
            return

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
        except Exception:
            logger.exception("Post-run analysis failed: %s", plan.run_dir)
            return

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
            _append_index_entry_locked(index_path, entry)
        except Exception:
            logger.exception("Failed to write index entry for: %s", plan.run_dir)


async def execute_all(
    plans: list[RunPlan],
    instances: dict[str, dict[str, Any]],
    *,
    index_path: Path | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> None:
    """Run every plan with up to ``concurrency`` in-flight; append to INDEX.jsonl.

    A single ``asyncio.Semaphore`` bounds concurrency; ``asyncio.gather`` with
    ``return_exceptions=True`` ensures one run's unexpected failure does not
    cancel the others. Every dispatched run records its outcome via
    ``_append_index_entry_locked`` so INDEX.jsonl stays consistent under
    concurrent writers.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    index_path = index_path or Path("dataset/INDEX.jsonl")
    index_path.parent.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(concurrency)
    logger.info(
        "Executing %d plans with concurrency=%d (index=%s)",
        len(plans),
        concurrency,
        index_path,
    )
    tasks = [_run_one(plan, instances, index_path, semaphore) for plan in plans]
    await asyncio.gather(*tasks, return_exceptions=True)


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
@click.option(
    "--concurrency",
    default=DEFAULT_CONCURRENCY,
    type=int,
    help=(
        "Max parallel (CVE, cell, replicate) runs. Default 5 (assumes Anthropic "
        "Tier 2+ and >=32GB host RAM). Set to 1 for strictly sequential."
    ),
)
def main(
    locked_instances: Path,
    cells: str,
    output_dir: Path,
    seed: int,
    concurrency: int,
) -> None:
    """Run the experiment over the (CVE x cell x replicate) Cartesian schedule."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Docker bind-mounts silently become NAMED VOLUMES for relative paths --
    # resolve once here so every downstream -v arg is guaranteed absolute.
    output_dir = output_dir.resolve()
    data = yaml.safe_load(locked_instances.read_text(encoding="utf-8"))
    # Pre-flight: fail loudly for malformed YAML entries before any run starts.
    for inst in data["instances"]:
        _validate_cve_data(inst, inst["instance_id"])
    # Pre-flight: warn (don't fail) for CVEs that can't be mechanically evaluated.
    _preflight_check_sanitizer_normalization(data["instances"])
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
    asyncio.run(execute_all(plans, instances, concurrency=concurrency))


if __name__ == "__main__":
    main()
