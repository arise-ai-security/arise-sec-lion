"""Basic metrics across all experiment cells (A1, A2, B1, B2) with pillar averages.

Goal
====
Produce summary tables covering all primary metrics for every experiment cell
and their pillar-level aggregates (A avg = flat cells, B avg = tree cells).
These tables serve as the single source of truth for top-level numbers quoted
in the report.

Metrics computed per run:

* **Outcome**: builder/exploiter/fixer/end_to_end pass (mechanical evaluator,
  with corrected_mechanical.json priority for A cells).
* **Cost & resources**: total_cost_usd (from INDEX), wallclock_seconds,
  event_count.
* **Tokens**: input_tokens, output_tokens, cache_read_tokens — summed from
  ``tokens_consumed`` events (tree-side LLM: BOSS/MANAGER/condenser) PLUS
  ``worker_cost_recorded`` events (Claude Agent SDK worker sessions). For A
  cells, a single ``tokens_consumed`` event covers the entire CLI session and
  ``worker_cost_recorded`` does not exist.
* **Tool activity**: tool_calls_total, security_tool_adoption,
  security_tool_invocations — parsed from ``tool_use`` events.
* **Redundancy**: intra/sibling/hierarchy counts and redundancy_rate_total —
  the (tool, target) pair-level triad from the pre-registered R1 metric.
* **Audit**: audit_violations from INDEX.
* **Deliverables**: workspace_has_patch — filesystem scan for patch files.
* **Termination**: termination_reason distribution, completed_rate.

Inputs
------
* ``dataset-final/INDEX.jsonl`` — run-level summaries (``IndexEntry`` schema).
* ``dataset-final/runs/{cve}/{cell}/{rep}/events.jsonl`` — per-run event log.
* ``dataset-final/runs/{cve}/{cell}/{rep}/mechanical.json`` — evaluator result.
* ``dataset-final/runs/{cve}/{cell}/{rep}/corrected_mechanical.json`` — A-cell
  sensitivity correction (overrides ``mechanical.json`` when present).
* ``dataset-final/runs/{cve}/{cell}/{rep}/audit.json`` — cheating audit.
* ``experiments/configs/locked_instances.yaml`` — stratum mapping.

Outputs (under ``docs/artifacts/tables/basic_metrics/``)
--------------------------------------------------------
* ``cell_summary.csv`` — 6 rows (A1, A2, B1, B2, A_avg, B_avg).
* ``per_run.csv`` — 40 rows (10 CVEs x 4 cells, primary replicate only).
* ``termination.csv`` — termination_reason x cell cross-tab.

Design choices
==============
1. **A1 replicate policy: primary-only (rep=0).** A1 has 12 runs (anchor CVE
   has 3 replicates). Only replicate 0 is included to keep N=10 per cell.
2. **Pillar averages: pooled.** A_avg pools A1+A2 (N=20), B_avg pools B1+B2
   (N=20). Each run has equal weight.
3. **Effective pass priority:** corrected_mechanical.json > mechanical.json >
   INDEX.mechanical_pass. Only A cells have corrections.
4. **INDEX dedup: last-row-wins by run_id.**
5. **Token two-family sum.** ``tokens_consumed`` (tree LLM) +
   ``worker_cost_recorded`` (SDK workers). For A cells, only the former
   exists (single aggregate event per run).
6. **Deliverable scan.** A cells: ``workspace/``. B cells:
   ``<agent_uuid>/(testcase|src)/``. Stock files filtered out.

Run
---
    uv run python docs/artifacts/scripts/basic_metrics.py
"""

from __future__ import annotations

import csv
import json
import logging
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ROOT = REPO_ROOT / "dataset-final"
INDEX_PATH = DATASET_ROOT / "INDEX.jsonl"
RUNS_ROOT = DATASET_ROOT / "runs"
INSTANCES_PATH = REPO_ROOT / "experiments" / "configs" / "locked_instances.yaml"
OUT_DIR = REPO_ROOT / "docs" / "artifacts" / "tables" / "basic_metrics"

CELLS = ("A1", "A2", "B1", "B2")
A_CELLS = ("A1", "A2")
B_CELLS = ("B1", "B2")
PHASES = ("builder", "exploiter", "fixer", "end_to_end")

SECURITY_TOOLS: tuple[str, ...] = ("valgrind", "klee")

PATCH_NAME_RE: re.Pattern[str] = re.compile(
    r"^(?:model_patch\.diff|fix\.patch|cve-[0-9\-]+\.patch|patch\.diff|.*_patch\.diff)$",
    re.IGNORECASE,
)

STOCK_FILE_NAMES: frozenset[str] = frozenset(
    {"llvmsymbol.diff", "repo_changes.diff", "base_commit_hash"}
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("basic_metrics")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Malformed JSON at %s -- treating as empty", path)
        return {}


def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    if not rows:
        logger.warning("No rows for %s; writing empty file.", path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = columns or sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    logger.info("Wrote %s (%d rows, %d cols)", path, len(rows), len(cols))


def _iter_events(run_dir: Path) -> Iterable[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _target_from_tool_input(tool_name: str, tool_input: dict) -> str | None:
    if not isinstance(tool_input, dict) or not tool_input:
        return None
    if tool_name in {"Read", "Edit", "Write"}:
        fp = tool_input.get("file_path")
        if isinstance(fp, str):
            return fp
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if isinstance(cmd, str):
            toks = cmd.strip().split()
            return " ".join(toks[:3])
    if tool_name in {"Grep", "Glob"}:
        pat = tool_input.get("pattern") or tool_input.get("query")
        if isinstance(pat, str):
            return pat
    return None


def load_stratum_map() -> dict[str, str]:
    data = yaml.safe_load(INSTANCES_PATH.read_text(encoding="utf-8"))
    return {inst["instance_id"]: inst["stratum"] for inst in data["instances"]}


def load_index() -> dict[str, dict]:
    if not INDEX_PATH.exists():
        raise FileNotFoundError(f"INDEX.jsonl not found at {INDEX_PATH}")
    latest: dict[str, dict] = {}
    for line in INDEX_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = row.get("run_id")
        if rid:
            latest[rid] = row
    return latest


# ---------------------------------------------------------------------------
# Deliverable detection
# ---------------------------------------------------------------------------

def _iter_deliverable_files(run_dir: Path, cell: str) -> Iterable[Path]:
    """Yield candidate deliverable files.

    A cells write to ``workspace/``. B cells write to per-agent
    ``<uuid>/(testcase|src)/`` directories.
    """
    if cell in A_CELLS:
        ws = run_dir / "workspace"
        if ws.is_dir():
            for p in ws.iterdir():
                if p.is_file():
                    yield p
    else:
        if not run_dir.exists():
            return
        for agent_dir in run_dir.iterdir():
            if not agent_dir.is_dir() or agent_dir.name == "workspace":
                continue
            for sub in ("testcase", "src"):
                sub_dir = agent_dir / sub
                if not sub_dir.is_dir():
                    continue
                for p in sub_dir.iterdir():
                    if p.is_file():
                        yield p


def _has_patch(run_dir: Path, cell: str) -> bool:
    for f in _iter_deliverable_files(run_dir, cell):
        if f.name in STOCK_FILE_NAMES:
            continue
        if PATCH_NAME_RE.match(f.name):
            return True
    return False


# ---------------------------------------------------------------------------
# Per-run metric container
# ---------------------------------------------------------------------------

@dataclass
class RunRecord:
    run_id: str
    cve_id: str
    cell: str
    replicate: int
    stratum: str
    project: str
    termination_reason: str
    wallclock_seconds: float
    total_cost_usd: float
    event_count: int
    effective_pass: dict[str, bool] = field(default_factory=dict)
    effective_pass_source: str = ""

    # Tokens (combined tokens_consumed + worker_cost_recorded)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    # Tool activity
    tool_calls_total: int = 0
    security_tool_adoption: bool = False
    security_tool_invocations: int = 0

    # Redundancy
    redundancy_intra: int = 0
    redundancy_sibling: int = 0
    redundancy_hierarchy: int = 0
    redundancy_rate_total: float | None = None

    # Audit
    audit_violations: int = 0

    # Deliverables
    workspace_has_patch: bool = False

    # Tree topology (B cells only; A cells remain 0)
    total_agents: int = 0
    agents_boss: int = 0
    agents_manager: int = 0
    agents_worker: int = 0
    max_depth: int = 0
    max_children_per_node: int = 0


# ---------------------------------------------------------------------------
# Effective pass resolution
# ---------------------------------------------------------------------------

def _resolve_effective_pass(
    index_row: dict,
    run_dir: Path,
) -> tuple[dict[str, bool], str]:
    corrected = _load_json(run_dir / "corrected_mechanical.json")
    if corrected:
        result = {}
        for phase in PHASES:
            key = f"{phase}_pass"
            v = corrected.get(key)
            result[phase] = bool(v) if isinstance(v, bool) else False
        return result, "corrected"

    mechanical = _load_json(run_dir / "mechanical.json")
    if mechanical:
        result = {}
        for phase in PHASES:
            key = f"{phase}_pass"
            v = mechanical.get(key)
            result[phase] = bool(v) if isinstance(v, bool) else False
        return result, "mechanical"

    mp = index_row.get("mechanical_pass") or {}
    return {phase: bool(mp.get(phase)) for phase in PHASES}, "index"


# ---------------------------------------------------------------------------
# Event-level metric computation
# ---------------------------------------------------------------------------

def _compute_event_metrics(run_dir: Path, record: RunRecord) -> None:
    """Parse events.jsonl and populate token, tool, redundancy, and topology fields."""
    per_agent_calls: dict[str, list[tuple[str, str]]] = defaultdict(list)
    agent_parent: dict[str, str | None] = {}
    agent_role: dict[str, str] = {}
    agent_depth: dict[str, int] = {}

    tool_counter: Counter[str] = Counter()
    security_hits = 0

    for ev in _iter_events(run_dir):
        et = ev.get("event_type")
        payload: dict = ev.get("payload") or {}

        if et == "agent_created":
            aid = ev.get("agent_id")
            if aid is not None:
                agent_parent[aid] = ev.get("parent_agent_id")
                agent_role[aid] = (
                    ev.get("role") or payload.get("role") or "?"
                ).upper()
                agent_depth[aid] = ev.get("depth", 0)

        elif et == "tokens_consumed":
            record.input_tokens += int(payload.get("input_tokens") or 0)
            record.output_tokens += int(payload.get("output_tokens") or 0)
            record.cache_read_tokens += int(
                payload.get("cache_read_input_tokens") or 0
            )

        elif et == "worker_cost_recorded":
            record.input_tokens += int(payload.get("prompt_tokens") or 0)
            record.output_tokens += int(payload.get("completion_tokens") or 0)
            record.cache_read_tokens += int(
                payload.get("cache_read_tokens") or 0
            )

        elif et == "tool_use":
            if isinstance(payload.get("data"), dict):
                continue
            tn = payload.get("tool_name")
            if not tn or tn in {"?", "Tool"}:
                continue
            tool_counter[tn] += 1

            ti = payload.get("tool_input") or {}
            target = _target_from_tool_input(tn, ti)
            aid = ev.get("agent_id") or "FLAT"
            if target is not None:
                per_agent_calls[aid].append((tn, target))

            if tn == "Bash":
                cmd = ti.get("command", "")
                if isinstance(cmd, str) and any(t in cmd for t in SECURITY_TOOLS):
                    security_hits += 1

    record.tool_calls_total = sum(tool_counter.values())
    record.security_tool_adoption = security_hits > 0
    record.security_tool_invocations = security_hits

    # --- Redundancy ---
    # Intra: same agent issues same (tool, target) more than once.
    intra = 0
    for calls in per_agent_calls.values():
        c = Counter(calls)
        intra += sum(v - 1 for v in c.values() if v > 1)
    record.redundancy_intra = intra

    # Sibling: children of the same parent each issue the same (tool, target).
    sibling = 0
    sibling_groups: dict[str | None, list[str]] = defaultdict(list)
    for aid, pid in agent_parent.items():
        sibling_groups[pid].append(aid)
    for pid, kids in sibling_groups.items():
        if pid is None or len(kids) < 2:
            continue
        pair_members: dict[tuple[str, str], set[str]] = defaultdict(set)
        for c in kids:
            for pair in per_agent_calls.get(c, []):
                pair_members[pair].add(c)
        for members in pair_members.values():
            if len(members) >= 2:
                sibling += len(members) - 1
    record.redundancy_sibling = sibling

    # Hierarchy: descendant re-issues an ancestor's (tool, target).
    def ancestors(aid: str) -> list[str]:
        out, cur = [], agent_parent.get(aid)
        while cur is not None:
            out.append(cur)
            cur = agent_parent.get(cur)
        return out

    hierarchy = 0
    for aid, calls in per_agent_calls.items():
        anc_pairs: set[tuple[str, str]] = set()
        for anc in ancestors(aid):
            anc_pairs.update(per_agent_calls.get(anc, []))
        for pair in calls:
            if pair in anc_pairs:
                hierarchy += 1
    record.redundancy_hierarchy = hierarchy

    if record.tool_calls_total:
        record.redundancy_rate_total = (
            (intra + sibling + hierarchy) / record.tool_calls_total
        )

    # --- Topology ---
    record.total_agents = len(agent_role)
    role_counts = Counter(agent_role.values())
    record.agents_boss = role_counts.get("BOSS", 0)
    record.agents_manager = role_counts.get("MANAGER", 0)
    record.agents_worker = role_counts.get("WORKER", 0)
    record.max_depth = max(agent_depth.values(), default=0)
    children_count: Counter[str] = Counter()
    for pid in agent_parent.values():
        if pid is not None:
            children_count[pid] += 1
    record.max_children_per_node = max(children_count.values(), default=0)


# ---------------------------------------------------------------------------
# Build records
# ---------------------------------------------------------------------------

def build_run_records(
    index_by_id: dict[str, dict], stratum_map: dict[str, str]
) -> list[RunRecord]:
    records: list[RunRecord] = []
    for row in index_by_id.values():
        cell = row.get("cell", "")
        if cell not in CELLS:
            continue
        if row.get("replicate", 0) != 0:
            continue

        cve_id = row["cve_id"]
        run_dir = Path(row.get("path") or RUNS_ROOT / cve_id / cell / "0")
        passes, source = _resolve_effective_pass(row, run_dir)
        project = cve_id.split(".")[0] if "." in cve_id else cve_id

        record = RunRecord(
            run_id=row["run_id"],
            cve_id=cve_id,
            cell=cell,
            replicate=0,
            stratum=stratum_map.get(cve_id, "UNKNOWN"),
            project=project,
            termination_reason=row.get("termination_reason", "unknown"),
            wallclock_seconds=float(row.get("wallclock_seconds", 0)),
            total_cost_usd=float(row.get("total_cost_usd", 0)),
            event_count=int(row.get("event_count", 0)),
            effective_pass=passes,
            effective_pass_source=source,
            audit_violations=int(row.get("audit_violations") or 0),
            workspace_has_patch=_has_patch(run_dir, cell),
        )
        _compute_event_metrics(run_dir, record)
        records.append(record)

    records.sort(key=lambda r: (r.cell, r.cve_id))
    return records


# ---------------------------------------------------------------------------
# Table builders
# ---------------------------------------------------------------------------

def _cell_system(cell: str) -> str:
    return "flat_cli" if cell in A_CELLS else "tree"


def _agg_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "median": None, "std": None, "min": None, "max": None}
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "std": statistics.stdev(values) if len(values) >= 2 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def build_cell_summary(records: list[RunRecord]) -> list[dict]:
    groups: dict[str, list[RunRecord]] = defaultdict(list)
    for r in records:
        groups[r.cell].append(r)

    pillar_groups = {
        "A_avg": [r for r in records if r.cell in A_CELLS],
        "B_avg": [r for r in records if r.cell in B_CELLS],
    }

    rows: list[dict] = []
    for label in list(CELLS) + ["A_avg", "B_avg"]:
        runs = groups.get(label) or pillar_groups.get(label, [])
        n = len(runs)

        row: dict[str, Any] = {"cell": label, "n": n}
        row["system"] = (
            _cell_system(label)
            if label in CELLS
            else ("flat_cli" if label == "A_avg" else "tree")
        )

        for phase in PHASES:
            pass_count = sum(1 for r in runs if r.effective_pass.get(phase))
            row[f"{phase}_pass_count"] = pass_count
            row[f"{phase}_pass_rate"] = pass_count / n if n else 0.0

        for attr, prefix in [
            ("total_cost_usd", "cost"),
            ("wallclock_seconds", "wallclock"),
        ]:
            vals = [getattr(r, attr) for r in runs]
            for k, v in _agg_stats(vals).items():
                row[f"{prefix}_{k}"] = v

        # Token means
        row["input_tokens_mean"] = statistics.fmean([r.input_tokens for r in runs]) if runs else None
        row["output_tokens_mean"] = statistics.fmean([r.output_tokens for r in runs]) if runs else None
        row["cache_read_tokens_mean"] = statistics.fmean([r.cache_read_tokens for r in runs]) if runs else None

        row["event_count_mean"] = statistics.fmean([r.event_count for r in runs]) if runs else None
        row["tool_calls_total_mean"] = statistics.fmean([r.tool_calls_total for r in runs]) if runs else None

        sec_adopters = sum(1 for r in runs if r.security_tool_adoption)
        row["security_tool_adoption_rate"] = sec_adopters / n if n else 0.0

        red_rates = [r.redundancy_rate_total for r in runs if r.redundancy_rate_total is not None]
        row["redundancy_rate_total_mean"] = statistics.fmean(red_rates) if red_rates else None

        row["audit_violations_mean"] = statistics.fmean([r.audit_violations for r in runs]) if runs else None

        patch_count = sum(1 for r in runs if r.workspace_has_patch)
        row["workspace_has_patch_rate"] = patch_count / n if n else 0.0

        # Topology
        row["total_agents_mean"] = statistics.fmean([r.total_agents for r in runs]) if runs else None
        row["agents_boss_mean"] = statistics.fmean([r.agents_boss for r in runs]) if runs else None
        row["agents_manager_mean"] = statistics.fmean([r.agents_manager for r in runs]) if runs else None
        row["agents_worker_mean"] = statistics.fmean([r.agents_worker for r in runs]) if runs else None
        row["max_depth_mean"] = statistics.fmean([r.max_depth for r in runs]) if runs else None
        row["max_children_per_node_mean"] = statistics.fmean([r.max_children_per_node for r in runs]) if runs else None

        completed = sum(1 for r in runs if r.termination_reason == "completed")
        row["completed_count"] = completed
        row["completed_rate"] = completed / n if n else 0.0

        rows.append(row)
    return rows


def build_per_run_rows(records: list[RunRecord]) -> list[dict]:
    rows: list[dict] = []
    for r in records:
        row: dict[str, Any] = {
            "cve_id": r.cve_id,
            "cell": r.cell,
            "stratum": r.stratum,
            "project": r.project,
            "termination_reason": r.termination_reason,
            "total_cost_usd": r.total_cost_usd,
            "wallclock_seconds": r.wallclock_seconds,
            "event_count": r.event_count,
            "effective_pass_source": r.effective_pass_source,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "tool_calls_total": r.tool_calls_total,
            "security_tool_adoption": r.security_tool_adoption,
            "security_tool_invocations": r.security_tool_invocations,
            "redundancy_intra": r.redundancy_intra,
            "redundancy_sibling": r.redundancy_sibling,
            "redundancy_hierarchy": r.redundancy_hierarchy,
            "redundancy_rate_total": r.redundancy_rate_total,
            "audit_violations": r.audit_violations,
            "workspace_has_patch": r.workspace_has_patch,
            "total_agents": r.total_agents,
            "agents_boss": r.agents_boss,
            "agents_manager": r.agents_manager,
            "agents_worker": r.agents_worker,
            "max_depth": r.max_depth,
            "max_children_per_node": r.max_children_per_node,
        }
        for phase in PHASES:
            row[f"{phase}_pass"] = r.effective_pass.get(phase, False)
        rows.append(row)
    return rows


def build_termination_rows(records: list[RunRecord]) -> list[dict]:
    by_cell: dict[str, Counter[str]] = {c: Counter() for c in list(CELLS) + ["A_avg", "B_avg"]}
    for r in records:
        by_cell[r.cell][r.termination_reason] += 1
        pillar = "A_avg" if r.cell in A_CELLS else "B_avg"
        by_cell[pillar][r.termination_reason] += 1

    all_reasons = sorted({reason for counts in by_cell.values() for reason in counts})
    rows: list[dict] = []
    for reason in all_reasons:
        row: dict[str, Any] = {"termination_reason": reason}
        for label in list(CELLS) + ["A_avg", "B_avg"]:
            total = sum(by_cell[label].values())
            count = by_cell[label].get(reason, 0)
            row[f"count_{label}"] = count
            row[f"share_{label}"] = count / total if total else 0.0
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Column orderings
# ---------------------------------------------------------------------------

CELL_SUMMARY_COLUMNS = [
    "cell", "n", "system",
    *[col for phase in PHASES for col in (f"{phase}_pass_count", f"{phase}_pass_rate")],
    "cost_mean", "cost_median", "cost_std", "cost_min", "cost_max",
    "wallclock_mean", "wallclock_median", "wallclock_std", "wallclock_min", "wallclock_max",
    "input_tokens_mean", "output_tokens_mean", "cache_read_tokens_mean",
    "event_count_mean", "tool_calls_total_mean",
    "security_tool_adoption_rate",
    "redundancy_rate_total_mean",
    "audit_violations_mean",
    "workspace_has_patch_rate",
    "total_agents_mean", "agents_boss_mean", "agents_manager_mean", "agents_worker_mean",
    "max_depth_mean", "max_children_per_node_mean",
    "completed_count", "completed_rate",
]

PER_RUN_COLUMNS = [
    "cve_id", "cell", "stratum", "project",
    *[f"{phase}_pass" for phase in PHASES],
    "effective_pass_source",
    "total_cost_usd", "wallclock_seconds", "event_count",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "tool_calls_total",
    "security_tool_adoption", "security_tool_invocations",
    "redundancy_intra", "redundancy_sibling", "redundancy_hierarchy",
    "redundancy_rate_total",
    "audit_violations", "workspace_has_patch",
    "total_agents", "agents_boss", "agents_manager", "agents_worker",
    "max_depth", "max_children_per_node",
    "termination_reason",
]

TERMINATION_COLUMNS = [
    "termination_reason",
    *[col for label in list(CELLS) + ["A_avg", "B_avg"] for col in (f"count_{label}", f"share_{label}")],
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    stratum_map = load_stratum_map()
    index_by_id = load_index()
    logger.info("Loaded %d unique runs from %s", len(index_by_id), INDEX_PATH)

    records = build_run_records(index_by_id, stratum_map)
    logger.info(
        "Built %d primary-replicate records: %s",
        len(records),
        {c: sum(1 for r in records if r.cell == c) for c in CELLS},
    )

    cell_summary = build_cell_summary(records)
    _write_csv(OUT_DIR / "cell_summary.csv", cell_summary, CELL_SUMMARY_COLUMNS)

    per_run = build_per_run_rows(records)
    _write_csv(OUT_DIR / "per_run.csv", per_run, PER_RUN_COLUMNS)

    termination = build_termination_rows(records)
    _write_csv(OUT_DIR / "termination.csv", termination, TERMINATION_COLUMNS)

    logger.info("All tables written to %s", OUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
