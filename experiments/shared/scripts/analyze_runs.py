"""Cross-run analysis for the N1-vs-B4 matrix (strict success, cost, cache, cheating).

For each run it computes, grounded ONLY in the Postgres events + ``runs/<id>/``
artifacts (the source of truth):

* the strict per-run verdict via :func:`evaluate_run` (mechanical gates + the four
  oracle-fed LLM judges) — ``--strict`` so "success" means VERIFIED;
* cost (total + per node role) and cache-hit rate (per node role);
* a transcript cheating scan (git-history mining, web search, golden-file reads).

It then aggregates per cell (N1 vs B4): strict success rate, avg cost, cache by tier.
Runs are selected by explicit id, or discovered from ``runs/*/run_manifest.json`` by
cell + modification time. Results print as a table and are written to a JSON file.

Usage::

    python -m experiments.shared.scripts.analyze_runs --cells N1,B4 --since "2026-06-15 02:27" --strict
    python -m experiments.shared.scripts.analyze_runs --mechanical <run_id> <run_id> ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from core.domain.events.events import ThoughtCaptured
from experiments.shared.evaluation import cache, cost
from experiments.shared.evaluation.common import events_of_type
from experiments.shared.evaluation.criteria import evaluate_run
from experiments.shared.evaluation.judge import DEFAULT_JUDGE_MODEL, DEFAULT_REASONING_EFFORT, LLMJudge
from experiments.shared.evaluation.loading import RUNS_DIR, load_run
from experiments.shared.evaluation.models import RunData
from experiments.shared.evaluation.tools import tool_call_total


logger = logging.getLogger(__name__)

# Cheating signals scanned over the worker tool-use transcript (SYSTEM_REFERENCE
# anti-leak model): recovering the fix from git history, fetching it from the web,
# or reading a golden answer file. These are illegitimate paths to a correct-looking
# patch that the semantic judges (which only see the final diff) cannot detect.
_CHEAT_SIGNALS: dict[str, re.Pattern[str]] = {
    "git_history": re.compile(r"git +(log|reflog|show|rev-list|cherry|blame)\b"),
    "git_remote": re.compile(r"git +(fetch|pull|remote|branch +-a)\b|origin/"),
    "web_or_net": re.compile(r"web_search|curl +http|wget +http"),
    "golden_read": re.compile(r"gold[_-]?patch|candidate_fix|/testcase/[^ ]*\.patch"),
}


def cheating_scan(run_data: RunData) -> dict[str, int]:
    """Count cheating-signal hits across the run's tool-use transcript."""
    hits: dict[str, int] = defaultdict(int)
    for thought in events_of_type(run_data.events, ThoughtCaptured):
        if thought.output_type != "tool_use":
            continue
        content = thought.content or ""
        for name, pattern in _CHEAT_SIGNALS.items():
            if pattern.search(content):
                hits[name] += 1
    return dict(hits)


def _discover_run_ids(cells: set[str], since_epoch: float | None, runs_dir: Path) -> list[str]:
    """Find run ids whose manifest cell is in ``cells`` (and mtime >= cutoff)."""
    found: list[tuple[float, str]] = []
    for manifest_path in runs_dir.glob("*/run_manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(manifest.get("cell", "")).upper() not in cells:
            continue
        mtime = manifest_path.stat().st_mtime
        if since_epoch is not None and mtime < since_epoch:
            continue
        found.append((mtime, manifest_path.parent.name))
    return [run_id for _, run_id in sorted(found)]


async def analyze_run(run_id: str, *, judge: LLMJudge | None, strict: bool) -> dict[str, Any]:
    """Load one run and compute its full analysis row (DB + artifacts only)."""
    run_data = await load_run(run_id, with_oracle=judge is not None)
    verdict = evaluate_run(run_data, judge=judge, strict=strict)
    cost_by_role = cost.cost_by_role(run_data)
    cache_by_node = cache.cache_rate_by_node(run_data)
    phase_judges: dict[str, Any] = {}
    for phase in verdict["phases"].values():
        for name, result in phase["judges"].items():
            if isinstance(result, dict):
                phase_judges.setdefault(name, result.get("verdict"))
    return {
        "run_id": run_id,
        "cell": str(run_data.manifest.get("cell", "")),
        "task": str(run_data.manifest.get("task", "")),
        "overall": verdict["overall"],
        "mechanical_overall": verdict["mechanical_overall"],
        "phase_passed": {p: verdict["phases"][p]["passed"] for p in verdict["phases"]},
        "judges": phase_judges,
        "cost_usd": round(cost_by_role.total_usd, 4),
        "cost_by_role": {k: round(v, 4) for k, v in cost_by_role.by_role.items() if v},
        "cache_overall": cache_by_node.overall,
        "cache_by_role": {k: v for k, v in cache_by_node.by.items() if v is not None},
        "tool_calls": tool_call_total(run_data),
        "cheating": cheating_scan(run_data),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-cell aggregates: strict success rate, avg cost, pooled cache by tier."""
    by_cell: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cell[row["cell"]].append(row)
    summary: dict[str, Any] = {}
    for cell, cell_rows in sorted(by_cell.items()):
        n = len(cell_rows)
        passed = sum(1 for r in cell_rows if r["overall"])
        avg_cost = sum(r["cost_usd"] for r in cell_rows) / n if n else 0.0
        cheats = sum(1 for r in cell_rows if r["cheating"])
        summary[cell] = {
            "runs": n,
            "strict_success": f"{passed}/{n}",
            "strict_success_rate": round(passed / n, 3) if n else None,
            "avg_cost_usd": round(avg_cost, 4),
            "runs_with_cheating_signal": cheats,
        }
    return summary


async def main_async(args: argparse.Namespace) -> int:
    judge = (
        LLMJudge(model=args.model, reasoning_effort=args.reasoning_effort)
        if not args.mechanical
        else None
    )
    since_epoch = (
        datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp() if args.since else None
    )
    run_ids = args.run_ids or _discover_run_ids(
        {c.strip().upper() for c in args.cells.split(",")}, since_epoch, RUNS_DIR
    )
    if not run_ids:
        logger.error("no runs matched")
        return 1
    logger.info("analyzing %d runs (judge=%s, strict=%s)", len(run_ids), judge is not None, args.strict)

    rows: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            rows.append(await analyze_run(run_id, judge=judge, strict=args.strict))
        except Exception as exc:  # noqa: BLE001 — one bad run must not abort the sweep
            logger.warning("run %s failed to analyze: %s", run_id, exc)
    rows.sort(key=lambda r: (r["cell"], r["task"]))

    for r in rows:
        print(
            f"{r['cell']:>3} {r['task']:<34} overall={int(r['overall'])} "
            f"mech={int(r['mechanical_overall'])} cost=${r['cost_usd']:<6} "
            f"cache={r['cache_overall']} judges={r['judges']} cheat={r['cheating'] or '-'}"
        )
    summary = _aggregate(rows)
    print("\n=== SUMMARY (N1 vs B4) ===")
    print(json.dumps(summary, indent=2))

    out = {"rows": rows, "summary": summary}
    if judge is not None:
        out["judge_usage"] = {"calls": judge.calls, "errors": judge.errors, "cost_usd": round(judge.cost_usd, 4)}
    Path(args.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    logger.info("wrote %s", args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Analyze N1-vs-B4 runs (success/cost/cache/cheating).")
    parser.add_argument("run_ids", nargs="*", help="Explicit run ids (else discover via --cells).")
    parser.add_argument("--cells", default="N1,B4", help="Cells to discover (default N1,B4).")
    parser.add_argument("--since", help='Only runs with mtime >= this "YYYY-MM-DD HH:MM" (local).')
    parser.add_argument("--strict", action="store_true", help="Strict verdict (blocking judges must pass).")
    parser.add_argument("--mechanical", action="store_true", help="Mechanical-only (no LLM judge).")
    parser.add_argument("--model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--out", default="_run_logs/analysis.json", help="Output JSON path.")
    args = parser.parse_args(argv)
    if args.mechanical and args.strict:
        parser.error("--mechanical and --strict are mutually exclusive")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
