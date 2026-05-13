"""Audit CSV-reported metrics against an independent recomputation.

The CSVs under ``experiments/<study>/reports/tables/`` are produced by
``collect.py``, which in turn calls :func:`metrics_from_events_jsonl`.
A naive audit that calls the same function would be tautological (read the
same file twice, get the same answer). To catch divergences the audit here
re-derives every per-run column from a deliberately *parallel* implementation
that walks ``events.jsonl`` line-by-line with no reuse of
``metrics_from_events``. The two code paths share only the on-disk JSONL
format; any drift between them surfaces as a discrepancy.

**Scope limitation.** This audit only covers integer columns derivable from
``events.jsonl`` (the postcondition counters, retry/redecomposition counts,
agent population, judge flags). Filesystem-derived booleans
(``builder_artifacts_present`` / ``exploiter_artifacts_present`` /
``fixer_artifacts_present``) and the raw-string ``run_terminated_status``
are NOT audited — they share their implementation with ``collect.py`` and
have no independent verification path here. Float costs are also out of
scope because they are subject to rounding semantics that differ slightly
between accumulation orders.

Exit code 0 if every (run_id, column) pair matches; nonzero if any mismatch
is found. The first ten mismatches are printed in a tabular form.

Usage::

    python -m experiments.shared.scripts.metrics_audit --study <study-id>
    python -m experiments.shared.scripts.metrics_audit --csv path/to/run_metrics.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


logger = logging.getLogger(__name__)


JUDGE_PASS_THRESHOLD = 60  # Mirrors verification_pipeline.py:87 (_JUDGE_PASS_THRESHOLD)
STRUCTURAL_CHECK_FEEDBACK = (
    "Passed structural checks (no success criteria defined for judge evaluation)"
)

# Columns the audit covers. Limited on purpose to the postcondition metrics
# this audit is meant to police; columns derived from filesystem state
# (`*_artifacts_present`, `run_terminated_status`, `artifacts_path`, etc.) or
# tracked elsewhere (`event_files`) are out of scope.
_AUDIT_INT_FIELDS = (
    "event_count",
    "prompt_sent_count",
    "tool_call_count",
    "probe_count",
    "tool_result_count",
    "thinking_event_count",
    "worker_output_event_count",
    "worker_cost_event_count",
    "run_completed_count",
    "judge_score",
    "judge_ran",
    "judge_passed",
    "structural_check_passed",
    "total_agents",
    "completed_agents",
    "failed_agents",
    "retry_count",
    "redecomposition_count",
)


def _audit_metrics_from_events_jsonl(  # noqa: PLR0912, PLR0915
    path: Path,
) -> dict[str, Any]:
    """Independent reimplementation of the postcondition metric subset.

    Intentionally parallel to ``run_metrics.metrics_from_events`` — does NOT
    import or reuse it. Same on-disk input → same numbers, but any divergence
    in either code path now surfaces here instead of staying invisible.
    """
    counters: dict[str, int] = defaultdict(int)
    judge_score: int = -1
    judge_ran = 0
    structural_check_passed = 0
    real_judge_score: int | None = None
    total_agents = -1
    completed_agents = -1
    failed_agents = -1

    if not path.is_file():
        return _empty_audit_row(judge_score)

    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("audit: malformed JSON line in %s, refusing", path)
                raise
            if not isinstance(event, dict):
                continue
            counters["event_count"] += 1
            etype = event.get("event_type") or ""

            if etype == "PromptSent":
                counters["prompt_sent_count"] += 1
            elif etype == "ThoughtCaptured":
                counters["worker_output_event_count"] += 1
                otype = event.get("output_type") or "output"
                if otype == "tool_use":
                    counters["tool_call_count"] += 1
                elif otype == "tool_result":
                    counters["tool_result_count"] += 1
                elif otype == "thinking":
                    counters["thinking_event_count"] += 1
            elif etype == "ProbeStarted":
                counters["probe_count"] += 1
            elif etype == "ProbeCompleted":
                counters["tool_result_count"] += 1
            elif etype == "VerificationPassed":
                score = int(event.get("score") or 0)
                judge_score = score
                feedback = str(event.get("feedback") or "")
                if feedback == STRUCTURAL_CHECK_FEEDBACK:
                    structural_check_passed = 1
                else:
                    judge_ran = 1
                    real_judge_score = score
            elif etype == "VerificationFailed":
                if str(event.get("failed_stage") or "") == "judge":
                    score = int(event.get("score") or 0)
                    judge_score = score
                    judge_ran = 1
                    real_judge_score = score
            elif etype == "RetryScheduled":
                counters["retry_count"] += 1
            elif etype == "RedecompositionTriggered":
                counters["redecomposition_count"] += 1
            elif etype == "WorkerCostRecorded":
                counters["worker_cost_event_count"] += 1
            elif etype == "RunCompleted":
                counters["run_completed_count"] += 1
                try:
                    total_agents = int(event.get("total_agents") or 0)
                    completed_agents = int(event.get("completed_agents") or 0)
                    failed_agents = int(event.get("failed_agents") or 0)
                except (TypeError, ValueError):
                    pass

    judge_passed = 0
    if judge_ran == 1 and real_judge_score is not None:
        judge_passed = 1 if real_judge_score >= JUDGE_PASS_THRESHOLD else 0

    row: dict[str, Any] = {field: int(counters.get(field, 0)) for field in _AUDIT_INT_FIELDS}
    row["judge_score"] = int(judge_score)
    row["judge_ran"] = int(judge_ran)
    row["judge_passed"] = int(judge_passed)
    row["structural_check_passed"] = int(structural_check_passed)
    row["total_agents"] = int(total_agents)
    row["completed_agents"] = int(completed_agents)
    row["failed_agents"] = int(failed_agents)
    return row


def _empty_audit_row(judge_score: int) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(_AUDIT_INT_FIELDS, 0)
    row["judge_score"] = judge_score
    row["total_agents"] = -1
    row["completed_agents"] = -1
    row["failed_agents"] = -1
    return row


def _csv_int(value: str) -> int:
    """Coerce a CSV cell to int, treating empty/non-numeric as 0."""
    if not value:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            # Fall through floats like "10.0" written for older sentinels.
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def audit_csv(csv_path: Path) -> list[dict[str, Any]]:
    """Return a list of discrepancies between ``csv_path`` and a recomputation.

    Each discrepancy is a dict with keys
    ``run_id`` / ``column`` / ``csv_value`` / ``recomputed_value``.
    An empty list means the CSV is consistent with the independent recompute.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(csv_path)

    discrepancies: list[dict[str, Any]] = []

    with csv_path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            run_id = row.get("run_id") or ""
            events_jsonl = row.get("events_jsonl") or ""
            if not events_jsonl:
                # No events file (e.g. projection_status=failed) — skip.
                continue
            jsonl_path = Path(events_jsonl)
            if not jsonl_path.is_file():
                logger.warning(
                    "audit: events.jsonl missing for run %s (%s)",
                    run_id,
                    events_jsonl,
                )
                continue
            recomputed = _audit_metrics_from_events_jsonl(jsonl_path)
            for column, recomputed_value in recomputed.items():
                csv_value = _csv_int(row.get(column, ""))
                if csv_value != recomputed_value:
                    discrepancies.append({
                        "run_id": run_id,
                        "column": column,
                        "csv_value": csv_value,
                        "recomputed_value": recomputed_value,
                    })
    return discrepancies


def _format_discrepancies(discrepancies: list[dict[str, Any]]) -> str:
    if not discrepancies:
        return "no discrepancies"
    lines = [
        f"{'run_id':40s} {'column':32s} {'csv':>10s} {'recomputed':>10s}",
        "-" * 96,
    ]
    for diff in discrepancies[:10]:
        lines.append(
            f"{str(diff['run_id'])[:40]:40s} "
            f"{str(diff['column'])[:32]:32s} "
            f"{int(diff['csv_value']):>10d} "
            f"{int(diff['recomputed_value']):>10d}"
        )
    if len(discrepancies) > 10:
        lines.append(f"... and {len(discrepancies) - 10} more")
    return "\n".join(lines)


def _study_csv_path(study_id: str) -> Path:
    return (
        _REPO_ROOT
        / "experiments"
        / study_id
        / "reports"
        / "tables"
        / "run_metrics.csv"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metrics_audit")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--study", help="Study id (e.g. 2026-05-11-fresh-start)")
    group.add_argument("--csv", type=Path, help="Explicit path to run_metrics.csv")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    csv_path = _study_csv_path(args.study) if args.study else args.csv
    discrepancies = audit_csv(csv_path)
    if discrepancies:
        sys.stdout.write(f"{len(discrepancies)} discrepancies found\n")
        sys.stdout.write(_format_discrepancies(discrepancies))
        sys.stdout.write("\n")
        return 1
    sys.stdout.write("OK: 0 discrepancies\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
