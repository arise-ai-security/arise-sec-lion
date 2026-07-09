"""Generate the B1 study analysis report from host-local CSV + runs tarball.

B1 differs from A1/A2 in three ways and is otherwise apple-to-apple:

1. **Event source.** B1 events live in ``~/b1_export/b1_events.csv.gz``
   rather than the Postgres ``events`` table. The CSV-vs-events.jsonl
   cross-check replaces the DB-vs-runs cross-check.

2. **Single cell.** B1 has only one experimental cell (``B1``). There is
   no paired inference section.

3. **Three-tier hierarchy.** B1 uses BOSS -> MANAGER -> WORKER orchestration
   (depth 0 / 1 / 2 in the ``AgentCreated`` parent tree). The report
   surfaces per-role tool-call totals, averages, and composition; A1/A2
   are degenerate (boss-only) and ignore the metric.

All per-run computations reuse helpers from
:mod:`experiments.shared.scripts.a12_analysis` to keep the two reports
comparable. Anything that intentionally differs (event source, cell
structure, hierarchy metric) is implemented here.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from experiments.shared.scripts._paths import get_repo_root, to_repo_relative
from experiments.shared.scripts.a12_analysis import (
    CellSummary,
    EnrollmentEntry,
    RunFiles,
    RunRow,
    _cheat_analysis,
    _environmental_failure_cause,
    _failure_analysis,
    _is_auth_failure,
    _mean_or_none,
    _median_or_none,
    _per_run_breakdown,
    _prompt_samples,
    _round_nested,
    _tool_use_rows,
    compute_result_evidence,
    compute_tool_audit,
    read_run_events,
    read_run_manifest,
    wilson_interval,
)
from experiments.shared.scripts.analysis.metrics.cost import compute_cost
from experiments.shared.scripts.analysis.metrics.hierarchy import (
    HierarchyToolMetrics,
    compute_hierarchy_tool_calls,
)
from experiments.shared.scripts.analysis.run_result import (
    compute_run_result_from_events,
)
from experiments.shared.scripts.db.models import EventRow
from experiments.shared.scripts.write_report import write_binary, write_md


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

STUDY_ID = "b1-batch-autogen"
CELL = "B1"
CELLS: tuple[str, ...] = (CELL,)
REPORT_BASENAME = "b1_analysis"
TEMPLATE_PATH = "experiments/shared/templates/b1-analysis-report.md.j2"
DEFAULT_EVENTS_CSV = "~/b1_export/b1_events.csv.gz"
DEFAULT_RUNS_ROOT = "~/b1_export/runs"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")
SUCCESS_STATUS = "completed"
COST_ABS_TOLERANCE_USD = 1e-6
COST_REL_TOLERANCE = 1e-9
EVENT_TOP_LEVEL_KEYS = frozenset(
    {
        "event_id",
        "aggregate_id",
        "sequence_number",
        "event_type",
        "occurred_at",
        "metadata",
    }
)


# ---------------------------------------------------------------------------
# B1-specific dataclasses (hierarchy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HierarchyRow:
    """Per-run hierarchical tool-call breakdown (BOSS / MANAGER / WORKER)."""

    run_id: str
    boss_nodes: int
    manager_nodes: int
    worker_nodes: int
    boss_tool_calls: int
    manager_tool_calls: int
    worker_tool_calls: int
    boss_tool_composition_json: str
    manager_tool_composition_json: str
    worker_tool_composition_json: str


@dataclass(frozen=True)
class HierarchyCellSummary:
    """Per-cell aggregate over :class:`HierarchyRow` rows."""

    cell: str
    n_runs: int
    boss_nodes_total: int
    manager_nodes_total: int
    worker_nodes_total: int
    boss_tool_calls_total: int
    manager_tool_calls_total: int
    worker_tool_calls_total: int
    boss_tool_calls_mean_per_node: float | None
    manager_tool_calls_mean_per_node: float | None
    worker_tool_calls_mean_per_node: float | None
    boss_tool_calls_mean_per_run: float | None
    manager_tool_calls_mean_per_run: float | None
    worker_tool_calls_mean_per_run: float | None
    boss_tool_composition: dict[str, int]
    manager_tool_composition: dict[str, int]
    worker_tool_composition: dict[str, int]


# ---------------------------------------------------------------------------
# Paths and config
# ---------------------------------------------------------------------------


def _repo_path(*parts: str) -> Path:
    return get_repo_root().joinpath(*parts)


def _b1_runs_root() -> Path:
    from pathlib import Path as _Path

    return _Path(DEFAULT_RUNS_ROOT).expanduser()


def _b1_default_csv_path() -> Path:
    from pathlib import Path as _Path

    return _Path(DEFAULT_EVENTS_CSV).expanduser()


# ---------------------------------------------------------------------------
# CSV event loader
# ---------------------------------------------------------------------------


def load_b1_events_by_run(csv_path: Path) -> dict[str, list[EventRow]]:
    """Group B1 events by the run root (transitive parent walk).

    The CSV mirrors the Postgres ``events`` table columns 1:1
    (event_id, aggregate_id, sequence_number, event_type, payload,
    occurred_at, metadata). Returns a dict keyed by the run-root
    aggregate id (as string), where each value is the events for that
    boss-rooted subtree, ordered by ``(occurred_at, sequence_number)``.
    """
    parent_of: dict[str, str | None] = {}
    events_by_agg: dict[str, list[EventRow]] = defaultdict(list)
    run_roots: set[str] = set()

    with gzip.open(csv_path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"event_id={row.get('event_id')!r} has unparsable payload",
                ) from exc
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            event = EventRow(
                event_id=UUID(row["event_id"]),
                aggregate_id=UUID(row["aggregate_id"]),
                sequence_number=int(row["sequence_number"]),
                event_type=row["event_type"],
                payload=payload,
                occurred_at=_parse_csv_datetime(row["occurred_at"]),
                metadata=metadata,
            )
            agg_str = str(event.aggregate_id)
            events_by_agg[agg_str].append(event)
            if event.event_type == "AgentCreated":
                p = payload.get("parent_id")
                parent_of[agg_str] = str(p) if isinstance(p, str) and p else None
            elif event.event_type == "RunStarted":
                run_roots.add(agg_str)

    # Walk each aggregate to its root.
    root_memo: dict[str, str] = {}

    def root_of(agg: str) -> str:
        if agg in root_memo:
            return root_memo[agg]
        path: list[str] = []
        node: str | None = agg
        seen: set[str] = set()
        while node is not None and node not in seen and node not in root_memo:
            seen.add(node)
            path.append(node)
            node = parent_of.get(node)
        anchor: str
        if node is None:
            anchor = path[-1]
        elif node in root_memo:
            anchor = root_memo[node]
        else:
            anchor = node
        for p in path:
            root_memo[p] = anchor
        return root_memo[agg]

    grouped: dict[str, list[EventRow]] = defaultdict(list)
    for agg, agg_events in events_by_agg.items():
        root = root_of(agg)
        grouped[root].extend(agg_events)

    # Sort each subtree by (occurred_at, sequence_number).
    for events_list in grouped.values():
        events_list.sort(key=lambda e: (e.occurred_at, e.sequence_number))

    # Return only roots that actually emitted RunStarted.
    return {root: grouped[root] for root in run_roots if root in grouped}


def _parse_csv_datetime(value: str) -> datetime:
    """Parse the events.csv occurred_at field (ISO 8601 with TZ)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Cohort discovery from run manifests
# ---------------------------------------------------------------------------


def discover_b1_run_files(runs_root: Path) -> dict[str, RunFiles]:
    """Map run_id -> RunFiles for every B1 manifest under ``runs_root``."""
    files_by_run_id: dict[str, RunFiles] = {}
    if not runs_root.is_dir():
        return files_by_run_id
    for manifest_path in runs_root.glob("*/run_manifest.json"):
        try:
            manifest = read_run_manifest(manifest_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("skipping unreadable manifest %s: %s", manifest_path, exc)
            continue
        if manifest.get("study_id") != STUDY_ID or manifest.get("cell") != "B1":
            continue
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str):
            continue
        events_path = manifest_path.parent / "events.jsonl"
        if not events_path.is_file():
            logger.warning("manifest %s has no events.jsonl; skipping", manifest_path)
            continue
        files_by_run_id[run_id] = RunFiles(
            manifest_path=manifest_path, events_path=events_path,
        )
    return files_by_run_id


def _entry_from_manifest(manifest: dict[str, Any]) -> EnrollmentEntry:
    return EnrollmentEntry(
        run_id=str(manifest["run_id"]),
        cell=str(manifest["cell"]),
        task=str(manifest["task"]),
        replicate=int(manifest.get("replicate", 0)),
        started_at=str(manifest.get("started_at", "")),
    )


# ---------------------------------------------------------------------------
# Cross-validation helpers (CSV vs events.jsonl)
# ---------------------------------------------------------------------------


def _event_record(event: EventRow) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "aggregate_id": str(event.aggregate_id),
        "sequence_number": event.sequence_number,
        "event_type": event.event_type,
        "occurred_at": _iso_datetime(event.occurred_at),
        "payload": {
            k: v for k, v in event.payload.items() if k not in EVENT_TOP_LEVEL_KEYS
        },
        "metadata": event.metadata,
    }


def _event_ids(events: list[EventRow]) -> list[str]:
    return [str(event.event_id) for event in events]


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _events_records_match(left: list[EventRow], right: list[EventRow]) -> bool:
    return [_event_record(e) for e in left] == [_event_record(e) for e in right]


def _events_costs_match(left: list[EventRow], right: list[EventRow]) -> bool:
    left_cost = compute_cost(left)
    right_cost = compute_cost(right)
    return math.isclose(
        left_cost.total_cost_usd,
        right_cost.total_cost_usd,
        rel_tol=COST_REL_TOLERANCE,
        abs_tol=COST_ABS_TOLERANCE_USD,
    )


# ---------------------------------------------------------------------------
# Hierarchy aggregation
# ---------------------------------------------------------------------------


def _hierarchy_row(run_id: str, metrics: HierarchyToolMetrics) -> HierarchyRow:
    return HierarchyRow(
        run_id=run_id,
        boss_nodes=metrics.nodes_by_role.get("boss", 0),
        manager_nodes=metrics.nodes_by_role.get("manager", 0),
        worker_nodes=metrics.nodes_by_role.get("worker", 0),
        boss_tool_calls=metrics.tool_calls_by_role.get("boss", 0),
        manager_tool_calls=metrics.tool_calls_by_role.get("manager", 0),
        worker_tool_calls=metrics.tool_calls_by_role.get("worker", 0),
        boss_tool_composition_json=json.dumps(
            metrics.tool_calls_by_role_by_name.get("boss", {}), sort_keys=True,
        ),
        manager_tool_composition_json=json.dumps(
            metrics.tool_calls_by_role_by_name.get("manager", {}), sort_keys=True,
        ),
        worker_tool_composition_json=json.dumps(
            metrics.tool_calls_by_role_by_name.get("worker", {}), sort_keys=True,
        ),
    )


def summarize_hierarchy(
    cell: str,
    hierarchy_rows: list[HierarchyRow],
    raw_metrics: list[HierarchyToolMetrics],
) -> HierarchyCellSummary:
    n_runs = len(hierarchy_rows)
    boss_nodes_total = sum(r.boss_nodes for r in hierarchy_rows)
    manager_nodes_total = sum(r.manager_nodes for r in hierarchy_rows)
    worker_nodes_total = sum(r.worker_nodes for r in hierarchy_rows)
    boss_calls_total = sum(r.boss_tool_calls for r in hierarchy_rows)
    manager_calls_total = sum(r.manager_tool_calls for r in hierarchy_rows)
    worker_calls_total = sum(r.worker_tool_calls for r in hierarchy_rows)

    def _per_node(calls: int, nodes: int) -> float | None:
        return calls / nodes if nodes else None

    def _per_run(calls: int) -> float | None:
        return calls / n_runs if n_runs else None

    boss_comp: dict[str, int] = defaultdict(int)
    manager_comp: dict[str, int] = defaultdict(int)
    worker_comp: dict[str, int] = defaultdict(int)
    for m in raw_metrics:
        for tool, n in m.tool_calls_by_role_by_name.get("boss", {}).items():
            boss_comp[tool] += n
        for tool, n in m.tool_calls_by_role_by_name.get("manager", {}).items():
            manager_comp[tool] += n
        for tool, n in m.tool_calls_by_role_by_name.get("worker", {}).items():
            worker_comp[tool] += n

    return HierarchyCellSummary(
        cell=cell,
        n_runs=n_runs,
        boss_nodes_total=boss_nodes_total,
        manager_nodes_total=manager_nodes_total,
        worker_nodes_total=worker_nodes_total,
        boss_tool_calls_total=boss_calls_total,
        manager_tool_calls_total=manager_calls_total,
        worker_tool_calls_total=worker_calls_total,
        boss_tool_calls_mean_per_node=_per_node(boss_calls_total, boss_nodes_total),
        manager_tool_calls_mean_per_node=_per_node(manager_calls_total, manager_nodes_total),
        worker_tool_calls_mean_per_node=_per_node(worker_calls_total, worker_nodes_total),
        boss_tool_calls_mean_per_run=_per_run(boss_calls_total),
        manager_tool_calls_mean_per_run=_per_run(manager_calls_total),
        worker_tool_calls_mean_per_run=_per_run(worker_calls_total),
        boss_tool_composition=dict(sorted(boss_comp.items(), key=lambda kv: -kv[1])),
        manager_tool_composition=dict(sorted(manager_comp.items(), key=lambda kv: -kv[1])),
        worker_tool_composition=dict(sorted(worker_comp.items(), key=lambda kv: -kv[1])),
    )


# ---------------------------------------------------------------------------
# Per-run row builder (apple-to-apple with a12.build_run_row)
# ---------------------------------------------------------------------------


def build_b1_run_row(
    csv_events: list[EventRow],
    entry: EnrollmentEntry,
    files: RunFiles,
) -> tuple[RunRow, HierarchyRow, HierarchyToolMetrics]:
    """Build the apple-to-apple :class:`RunRow` for one B1 run.

    Reuses a12 ``compute_result_evidence`` and ``compute_tool_audit``
    plus the shared ``compute_run_result_from_events`` so per-run
    quantitative metrics are computed by the same code paths as a12.

    Also returns the hierarchy tool-call metrics for this run.
    """
    run_uuid = UUID(entry.run_id)
    run_events = read_run_events(files.events_path)
    if not csv_events:
        raise ValueError(f"no CSV events for B1 run {entry.run_id}")

    event_ids_match = _event_ids(csv_events) == _event_ids(run_events)
    records_match = _events_records_match(csv_events, run_events)
    cost_match = _events_costs_match(csv_events, run_events)
    if not (event_ids_match and records_match and cost_match):
        raise ValueError(
            f"CSV/runs disagreement for {entry.run_id}: "
            f"ids={event_ids_match} records={records_match} cost={cost_match}",
        )

    result = compute_run_result_from_events(csv_events, run_uuid, family="A")
    quantitative = result.quantitative
    outcomes = quantitative.outcomes
    cost = quantitative.cost
    tools = quantitative.tools
    timing = quantitative.timing
    tool_audit = compute_tool_audit(csv_events, tools)
    result_evidence = compute_result_evidence(files.events_path.parent, csv_events)
    failure_reason = outcomes.failure_reason or ""
    failure_mode = outcomes.failure_mode or ""
    environmental_cause = _environmental_failure_cause(failure_reason, failure_mode)
    successful = (
        outcomes.has_run_completed
        and outcomes.has_work_completed
        and outcomes.run_status == SUCCESS_STATUS
    )
    real_pipeline_success = int(
        outcomes.has_run_completed
        and successful
        and result_evidence.builder_real_success
        and result_evidence.exploiter_real_success
        and result_evidence.fixer_real_success,
    )
    worker_cost_event_count = sum(
        1 for e in csv_events if e.event_type == "WorkerCostRecorded"
    )
    has_observed_cost = worker_cost_event_count > 0 or cost.manager_cost_usd > 0.0
    started_at = entry.started_at or _iso_datetime(csv_events[0].occurred_at)

    row = RunRow(
        source_authority="csv_events_verified_against_runs",
        run_id=entry.run_id,
        cell=entry.cell,
        task=entry.task,
        replicate=entry.replicate,
        started_at=started_at,
        db_event_count=len(csv_events),
        runs_event_count=len(run_events),
        db_runs_event_ids_match=int(event_ids_match),
        db_runs_event_records_match=int(records_match),
        db_runs_cost_match=int(cost_match),
        terminal=int(outcomes.has_run_completed),
        successful=int(successful),
        run_status=outcomes.run_status,
        failure_mode=failure_mode,
        failure_reason=failure_reason,
        auth_failure=int(_is_auth_failure(failure_reason)),
        environmental_failure=int(environmental_cause != ""),
        environmental_failure_cause=environmental_cause,
        manager_cost_usd=cost.manager_cost_usd,
        worker_cost_usd=cost.worker_cost_usd,
        total_cost_usd=cost.total_cost_usd,
        has_observed_cost=int(has_observed_cost),
        total_tokens=cost.total_tokens,
        prompt_tokens=cost.prompt_tokens,
        completion_tokens=cost.completion_tokens,
        cache_read_tokens=cost.cache_read_tokens,
        cache_write_tokens=cost.cache_write_tokens,
        reasoning_tokens=cost.reasoning_tokens,
        run_duration_seconds=timing.run_duration_seconds,
        wall_clock_seconds=timing.wall_clock_seconds,
        total_tool_calls=tools.total_tool_calls,
        bash_tool_calls=tools.by_tool_name.get("Bash", 0),
        task_mgmt_tool_calls=tools.by_category.get("task_mgmt", 0),
        task_tool_calls=tools.subagent_spawn_count,
        forbidden_web_attempts=tools.forbidden_web_attempts,
        worker_cost_event_count=worker_cost_event_count,
        aggregate_count=quantitative.aggregate_count,
        actual_tool_calls=tool_audit.actual_tool_calls,
        cheat_tool_calls=tool_audit.cheat_tool_calls,
        cheat_git_log_calls=tool_audit.cheat_git_log_calls,
        cheat_git_show_calls=tool_audit.cheat_git_show_calls,
        cheat_git_reflog_calls=tool_audit.cheat_git_reflog_calls,
        cheat_git_diff_history_ref_calls=tool_audit.cheat_git_diff_history_ref_calls,
        cheat_git_diff_unparsed_calls=tool_audit.cheat_git_diff_unparsed_calls,
        recon_tool_calls=tool_audit.recon_tool_calls,
        security_tool_calls=tool_audit.security_tool_calls,
        subagent_tool_calls=tool_audit.subagent_tool_calls,
        task_family_tool_calls=tool_audit.task_family_tool_calls,
        task_create_tool_calls=tool_audit.task_create_tool_calls,
        task_update_tool_calls=tool_audit.task_update_tool_calls,
        task_list_tool_calls=tool_audit.task_list_tool_calls,
        file_read_tool_calls=tool_audit.file_read_tool_calls,
        file_write_tool_calls=tool_audit.file_write_tool_calls,
        file_edit_tool_calls=tool_audit.file_edit_tool_calls,
        search_tool_calls=tool_audit.search_tool_calls,
        shell_tool_calls=tool_audit.shell_tool_calls,
        web_forbidden_tool_calls=tool_audit.web_forbidden_tool_calls,
        mcp_tool_calls=tool_audit.mcp_tool_calls,
        other_tool_calls=tool_audit.other_tool_calls,
        bash_recon_tool_calls=tool_audit.bash_recon_tool_calls,
        bash_exploit_tool_calls=tool_audit.bash_exploit_tool_calls,
        bash_security_scan_tool_calls=tool_audit.bash_security_scan_tool_calls,
        bash_build_tool_calls=tool_audit.bash_build_tool_calls,
        bash_test_exec_tool_calls=tool_audit.bash_test_exec_tool_calls,
        bash_git_tool_calls=tool_audit.bash_git_tool_calls,
        bash_other_shell_tool_calls=tool_audit.bash_other_shell_tool_calls,
        builder_real_success=result_evidence.builder_real_success,
        builder_evidence_present=result_evidence.builder_evidence_present,
        builder_executable_count=result_evidence.builder_executable_count,
        builder_executables=result_evidence.builder_executables,
        builder_work_bin_executable_count=result_evidence.builder_work_bin_executable_count,
        builder_work_bin_executables=result_evidence.builder_work_bin_executables,
        builder_src_executable_count=result_evidence.builder_src_executable_count,
        builder_src_executables=result_evidence.builder_src_executables,
        exploit_validation_present=result_evidence.exploit_validation_present,
        exploit_verdict_pass=result_evidence.exploit_verdict_pass,
        exploit_determinism_pass=result_evidence.exploit_determinism_pass,
        exploit_error_match=result_evidence.exploit_error_match,
        exploit_crash_function_match=result_evidence.exploit_crash_function_match,
        exploiter_real_success=result_evidence.exploiter_real_success,
        exploit_expected_error=result_evidence.exploit_expected_error,
        exploit_observed_error=result_evidence.exploit_observed_error,
        exploit_expected_crash_function=result_evidence.exploit_expected_crash_function,
        exploit_observed_crash_function=result_evidence.exploit_observed_crash_function,
        exploit_determinism_runs=result_evidence.exploit_determinism_runs,
        patch_validation_present=result_evidence.patch_validation_present,
        patch_verdict_pass=result_evidence.patch_verdict_pass,
        patch_apply_clean=result_evidence.patch_apply_clean,
        patch_build_success=result_evidence.patch_build_success,
        patch_post_error_none=result_evidence.patch_post_error_none,
        patch_repro_runs_no_crash=result_evidence.patch_repro_runs_no_crash,
        fixer_real_success=result_evidence.fixer_real_success,
        patch_post_error=result_evidence.patch_post_error,
        patch_repro_runs=result_evidence.patch_repro_runs,
        patched_files=result_evidence.patched_files,
        model_patch_present=result_evidence.model_patch_present,
        repro_script_present=result_evidence.repro_script_present,
        security_report_present=result_evidence.security_report_present,
        real_pipeline_success=real_pipeline_success,
    )

    hierarchy_metrics = compute_hierarchy_tool_calls(csv_events, run_id=run_uuid)
    hierarchy_row = _hierarchy_row(entry.run_id, hierarchy_metrics)
    return row, hierarchy_row, hierarchy_metrics


# ---------------------------------------------------------------------------
# Cell summary (single B1 cell)
# ---------------------------------------------------------------------------


def summarize_b1_cell(rows: list[RunRow]) -> CellSummary:
    """Mirror of a12.summarize_cells for a single cell."""
    n = len(rows)
    if not rows:
        return CellSummary(
            cell="B1", n=0, terminal=0, nonterminal=0, successful=0,
            success_rate=0.0, success_rate_ci_low=0.0, success_rate_ci_high=0.0,
            terminal_success_rate=0.0, terminal_success_rate_ci_low=0.0,
            terminal_success_rate_ci_high=0.0,
            auth_failures=0, environmental_failures=0, auth_login_failures=0,
            credit_quota_failures=0, provider_api_failures=0,
            total_cost_usd=0.0, manager_cost_usd=0.0, worker_cost_usd=0.0,
            mean_cost_usd=None, median_cost_usd=None, cost_per_success_usd=None,
            cost_observed_runs=0, positive_cost_runs=0, zero_cost_event_runs=0,
            missing_cost_runs=0, total_tokens=0, mean_tokens=None,
            median_duration_seconds=None, total_tool_calls=0, mean_tool_calls=None,
            task_mgmt_tool_calls=0, task_tool_calls=0, forbidden_web_attempts=0,
        )
    cell = rows[0].cell
    successes = sum(r.successful for r in rows)
    terminal = sum(r.terminal for r in rows)
    ci_low, ci_high = wilson_interval(successes, n)
    term_ci_low, term_ci_high = wilson_interval(successes, terminal)
    cost_observed = [r for r in rows if r.has_observed_cost]
    positive_cost = [r for r in cost_observed if r.total_cost_usd > 0.0]
    zero_cost_event = [r for r in cost_observed if r.total_cost_usd == 0.0]
    costs = [r.total_cost_usd for r in cost_observed]
    tokens = [float(r.total_tokens) for r in cost_observed]
    durations = [
        r.run_duration_seconds for r in rows if r.run_duration_seconds is not None
    ]
    tool_calls = [float(r.total_tool_calls) for r in rows]
    return CellSummary(
        cell=cell,
        n=n,
        terminal=terminal,
        nonterminal=n - terminal,
        successful=successes,
        success_rate=successes / n if n else 0.0,
        success_rate_ci_low=ci_low,
        success_rate_ci_high=ci_high,
        terminal_success_rate=successes / terminal if terminal else 0.0,
        terminal_success_rate_ci_low=term_ci_low,
        terminal_success_rate_ci_high=term_ci_high,
        auth_failures=sum(r.auth_failure for r in rows),
        environmental_failures=sum(r.environmental_failure for r in rows),
        auth_login_failures=sum(
            r.environmental_failure_cause == "auth_login" for r in rows
        ),
        credit_quota_failures=sum(
            r.environmental_failure_cause == "credit_quota" for r in rows
        ),
        provider_api_failures=sum(
            r.environmental_failure_cause == "provider_api" for r in rows
        ),
        total_cost_usd=sum(costs),
        manager_cost_usd=sum(r.manager_cost_usd for r in rows),
        worker_cost_usd=sum(r.worker_cost_usd for r in rows),
        mean_cost_usd=_mean_or_none(costs),
        median_cost_usd=_median_or_none(costs),
        cost_per_success_usd=sum(costs) / successes if successes else None,
        cost_observed_runs=len(cost_observed),
        positive_cost_runs=len(positive_cost),
        zero_cost_event_runs=len(zero_cost_event),
        missing_cost_runs=n - len(cost_observed),
        total_tokens=sum(r.total_tokens for r in cost_observed),
        mean_tokens=_mean_or_none(tokens),
        median_duration_seconds=_median_or_none(durations),
        total_tool_calls=sum(r.total_tool_calls for r in rows),
        mean_tool_calls=_mean_or_none(tool_calls),
        task_mgmt_tool_calls=sum(r.task_mgmt_tool_calls for r in rows),
        task_tool_calls=sum(r.task_tool_calls for r in rows),
        forbidden_web_attempts=sum(r.forbidden_web_attempts for r in rows),
        actual_tool_calls=sum(r.actual_tool_calls for r in rows),
        cheat_tool_calls=sum(r.cheat_tool_calls for r in rows),
        recon_tool_calls=sum(r.recon_tool_calls for r in rows),
        security_tool_calls=sum(r.security_tool_calls for r in rows),
        subagent_tool_calls=sum(r.subagent_tool_calls for r in rows),
        task_family_tool_calls=sum(r.task_family_tool_calls for r in rows),
        task_create_tool_calls=sum(r.task_create_tool_calls for r in rows),
        task_update_tool_calls=sum(r.task_update_tool_calls for r in rows),
        task_list_tool_calls=sum(r.task_list_tool_calls for r in rows),
        file_read_tool_calls=sum(r.file_read_tool_calls for r in rows),
        file_write_tool_calls=sum(r.file_write_tool_calls for r in rows),
        file_edit_tool_calls=sum(r.file_edit_tool_calls for r in rows),
        search_tool_calls=sum(r.search_tool_calls for r in rows),
        shell_tool_calls=sum(r.shell_tool_calls for r in rows),
        web_forbidden_tool_calls=sum(r.web_forbidden_tool_calls for r in rows),
        mcp_tool_calls=sum(r.mcp_tool_calls for r in rows),
        other_tool_calls=sum(r.other_tool_calls for r in rows),
        bash_recon_tool_calls=sum(r.bash_recon_tool_calls for r in rows),
        bash_exploit_tool_calls=sum(r.bash_exploit_tool_calls for r in rows),
        bash_security_scan_tool_calls=sum(r.bash_security_scan_tool_calls for r in rows),
        bash_build_tool_calls=sum(r.bash_build_tool_calls for r in rows),
        bash_test_exec_tool_calls=sum(r.bash_test_exec_tool_calls for r in rows),
        bash_git_tool_calls=sum(r.bash_git_tool_calls for r in rows),
        bash_other_shell_tool_calls=sum(r.bash_other_shell_tool_calls for r in rows),
        builder_real_successes=sum(r.builder_real_success for r in rows),
        builder_evidence_present=sum(r.builder_evidence_present for r in rows),
        builder_executable_runs=sum(r.builder_executable_count > 0 for r in rows),
        builder_executable_count=sum(r.builder_executable_count for r in rows),
        exploiter_real_successes=sum(r.exploiter_real_success for r in rows),
        exploit_validation_present=sum(r.exploit_validation_present for r in rows),
        exploit_verdict_pass=sum(r.exploit_verdict_pass for r in rows),
        exploit_error_match=sum(r.exploit_error_match for r in rows),
        exploit_crash_function_match=sum(r.exploit_crash_function_match for r in rows),
        fixer_real_successes=sum(r.fixer_real_success for r in rows),
        patch_validation_present=sum(r.patch_validation_present for r in rows),
        patch_verdict_pass=sum(r.patch_verdict_pass for r in rows),
        patch_apply_clean=sum(r.patch_apply_clean for r in rows),
        patch_build_success=sum(r.patch_build_success for r in rows),
        patch_post_error_none=sum(r.patch_post_error_none for r in rows),
        real_pipeline_successes=sum(r.real_pipeline_success for r in rows),
        model_patch_present=sum(r.model_patch_present for r in rows),
        repro_script_present=sum(r.repro_script_present for r in rows),
        security_report_present=sum(r.security_report_present for r in rows),
    )


# ---------------------------------------------------------------------------
# Summary builder + renderer
# ---------------------------------------------------------------------------


def build_b1_summary(
    *,
    rows: list[RunRow],
    entries: list[EnrollmentEntry],
    files_by_run_id: dict[str, RunFiles],
    cell_summary: CellSummary,
    hierarchy_summary: HierarchyCellSummary,
    dataset_task_count: int,
    csv_path: Path,
    events_csv_total: int,
    runs_with_manifest: int,
    runs_with_events_jsonl: int,
    csv_run_root_count: int,
    enrolled_runs: int,
) -> dict[str, Any]:
    cell_summaries = {CELL: cell_summary}
    summary = {
        "study_id": STUDY_ID,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "design": {
            "B1": "B1 batch-autogen — boss/manager/worker recursive orchestrator",
            "worker_model": "claude-sonnet-4-5-20250929",
            "source": "host-local CSV ∩ host-local runs tarball; no Postgres",
        },
        "data_integrity": {
            "source_scope": "csv_events_intersect_local_runs",
            "events_csv_path": str(csv_path),
            "events_csv_total_rows": events_csv_total,
            "csv_run_root_count": csv_run_root_count,
            "dataset_task_count": dataset_task_count,
            "runs_with_manifest": runs_with_manifest,
            "runs_with_events_jsonl": runs_with_events_jsonl,
            "enrolled_runs": enrolled_runs,
            "terminal_rows": sum(r.terminal for r in rows),
            "nonterminal_rows": sum(r.terminal == 0 for r in rows),
            "missing_cost_rows": sum(r.has_observed_cost == 0 for r in rows),
            "environmental_failures": sum(r.environmental_failure for r in rows),
            "csv_runs_event_id_mismatches": sum(
                r.db_runs_event_ids_match == 0 for r in rows
            ),
            "csv_runs_event_record_mismatches": sum(
                r.db_runs_event_records_match == 0 for r in rows
            ),
            "csv_runs_cost_mismatches": sum(r.db_runs_cost_match == 0 for r in rows),
        },
        "cells": {CELL: asdict(cell_summary)},
        "hierarchy": {CELL: asdict(hierarchy_summary)},
        # Apple-to-apple with a12: same helper functions, just ``cells=("B1",)``.
        "prompt_samples": _prompt_samples(entries, files_by_run_id, cells=CELLS),
        "tool_use_rows": _tool_use_rows(cell_summaries, cell_names=CELLS),
        "cheat_analysis": _cheat_analysis(rows, cells=CELLS),
        "failure_analysis": _failure_analysis(rows, cells=CELLS),
        "per_run_breakdown": _per_run_breakdown(rows),
    }
    return _round_nested(summary)


def render_b1_report(summary: dict[str, Any]) -> str:
    template_abs = _repo_path(TEMPLATE_PATH)
    env = Environment(
        loader=FileSystemLoader(str(template_abs.parent)),
        autoescape=False,  # noqa: S701 - markdown report, not HTML rendering.
        undefined=StrictUndefined,
    )

    def _percent(value: float | None) -> str:
        return "n/a" if value is None else f"{100 * value:.1f}%"

    def _num(value: float | int | None, digits: int = 2) -> str:
        if value is None:
            return "n/a"
        return f"{value:,.{digits}f}"

    def _usd(value: float | None) -> str:
        return "n/a" if value is None else f"${value:,.2f}"

    env.filters["percent"] = _percent
    env.filters["num"] = _num
    env.filters["usd"] = _usd
    template = env.get_template(template_abs.name)
    return template.render(summary=summary)


def _csv_bytes(rows: list[Any]) -> bytes:
    if not rows:
        return b""
    buffer = io.StringIO()
    fieldnames = list(asdict(rows[0]).keys())
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow(asdict(r))
    return buffer.getvalue().encode("utf-8")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_b1_outputs(
    *,
    rows: list[RunRow],
    hierarchy_rows: list[HierarchyRow],
    summary: dict[str, Any],
) -> dict[str, str]:
    reports_dir = _repo_path("experiments", STUDY_ID, "reports")
    metrics_path = reports_dir / f"{REPORT_BASENAME}_metrics.csv"
    hierarchy_path = reports_dir / f"{REPORT_BASENAME}_hierarchy.csv"
    summary_path = reports_dir / f"{REPORT_BASENAME}_summary.json"
    report_path = reports_dir / f"{REPORT_BASENAME}_report.md"

    inputs = [
        f"experiments/{STUDY_ID}/manifest.yaml",
        f"experiments/{STUDY_ID}/dataset.yaml",
        f"experiments/{STUDY_ID}/configs/B1-ours-claude-noverifier.yaml",
    ]
    write_binary(
        path=metrics_path, content=_csv_bytes(rows), script=__file__, inputs=inputs,
    )
    write_binary(
        path=hierarchy_path,
        content=_csv_bytes(hierarchy_rows),
        script=__file__,
        inputs=inputs,
    )
    write_binary(
        path=summary_path, content=_json_bytes(summary), script=__file__, inputs=inputs,
    )
    report_inputs = [metrics_path, hierarchy_path, summary_path]
    report_body = render_b1_report(summary)
    write_md(
        path=report_path,
        content=report_body,
        script=__file__,
        template=TEMPLATE_PATH,
        inputs=report_inputs,
    )
    return {
        "metrics": to_repo_relative(metrics_path),
        "hierarchy": to_repo_relative(hierarchy_path),
        "summary": to_repo_relative(summary_path),
        "report": to_repo_relative(report_path),
    }


# ---------------------------------------------------------------------------
# Top-level analysis entrypoint
# ---------------------------------------------------------------------------


def run_b1_analysis(
    *,
    csv_path: Path | None = None,
    runs_root: Path | None = None,
) -> dict[str, Any]:
    csv_path = csv_path or _b1_default_csv_path()
    runs_root = runs_root or _b1_runs_root()

    events_by_run = load_b1_events_by_run(csv_path)
    csv_total_rows = sum(len(v) for v in events_by_run.values())
    files_by_run_id = discover_b1_run_files(runs_root)

    # Cohort = CSV-rooted runs ∩ local manifests with events.jsonl.
    enrolled_run_ids = sorted(set(events_by_run) & set(files_by_run_id))
    if not enrolled_run_ids:
        raise RuntimeError(
            f"no B1 runs match between CSV ({csv_path}) and runs root ({runs_root})",
        )

    rows: list[RunRow] = []
    entries: list[EnrollmentEntry] = []
    hierarchy_rows: list[HierarchyRow] = []
    hierarchy_metrics_per_run: list[HierarchyToolMetrics] = []
    for idx, run_id in enumerate(enrolled_run_ids, start=1):
        manifest = read_run_manifest(files_by_run_id[run_id].manifest_path)
        entry = _entry_from_manifest(manifest)
        row, hierarchy_row, metrics = build_b1_run_row(
            events_by_run[run_id], entry, files_by_run_id[run_id],
        )
        rows.append(row)
        entries.append(entry)
        hierarchy_rows.append(hierarchy_row)
        hierarchy_metrics_per_run.append(metrics)
        if idx % 25 == 0:
            logger.info("processed %s/%s B1 runs", idx, len(enrolled_run_ids))

    cell_summary = summarize_b1_cell(rows)
    hierarchy_summary = summarize_hierarchy("B1", hierarchy_rows, hierarchy_metrics_per_run)

    # Dataset task count from the authoritative study input.
    import yaml as _yaml
    dataset_path = _repo_path("experiments", STUDY_ID, "dataset.yaml")
    with dataset_path.open(encoding="utf-8") as fh:
        dataset = _yaml.safe_load(fh) or {}
    dataset_task_count = len(dataset.get("default_cves") or [])

    summary = build_b1_summary(
        rows=rows,
        entries=entries,
        files_by_run_id=files_by_run_id,
        cell_summary=cell_summary,
        hierarchy_summary=hierarchy_summary,
        dataset_task_count=dataset_task_count,
        csv_path=csv_path,
        events_csv_total=csv_total_rows,
        runs_with_manifest=len(files_by_run_id),
        runs_with_events_jsonl=len(files_by_run_id),
        csv_run_root_count=len(events_by_run),
        enrolled_runs=len(rows),
    )
    outputs = write_b1_outputs(
        rows=rows, hierarchy_rows=hierarchy_rows, summary=summary,
    )
    return {"summary": summary, "outputs": outputs}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--events-csv",
        default=DEFAULT_EVENTS_CSV,
        help="Path to b1_events.csv.gz (default: ~/b1_export/b1_events.csv.gz)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper()))
    from pathlib import Path as _Path

    csv_path = _Path(args.events_csv).expanduser()
    result = run_b1_analysis(csv_path=csv_path)
    logger.info("wrote B1 analysis outputs: %s", result["outputs"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
