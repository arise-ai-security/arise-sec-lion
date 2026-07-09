"""Generate the A1/A2 Claude Code CLI analysis report from locked runs."""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import math
import random
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from statistics import mean, median
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from experiments.shared.scripts._paths import get_repo_root, to_repo_relative
from experiments.shared.scripts.analysis.metrics.cost import compute_cost
from experiments.shared.scripts.analysis.metrics.tools import (
    ToolCategory,
    classify_tool,
)
from experiments.shared.scripts.analysis.run_result import compute_run_result
from experiments.shared.scripts.analysis.text.cheating_detector import (
    _cheating_pattern,
    _extract_bash_command,
)
from experiments.shared.scripts.analysis.text.prefixes import recover_tool_name
from experiments.shared.scripts.db import fetch_run_events, open_connection
from experiments.shared.scripts.db.models import EventRow
from experiments.shared.scripts.load_runs import iter_run_manifests
from experiments.shared.scripts.write_report import write_binary, write_md


if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg


logger = logging.getLogger(__name__)

STUDY_ID = "a12-batch-autogen"
REPORT_BASENAME = "a12_claude_code_cli"
TEMPLATE_PATH = "experiments/shared/templates/a12-analysis-report.md.j2"
DEFAULT_STARTED_BEFORE = "2026-05-18T13:00:00Z"
LOCAL_TIMEZONE = ZoneInfo("America/New_York")
MAX_EVIDENCE_LIST_ITEMS = 12
PROMPT_SAMPLE_CHAR_LIMIT = 3000
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
SUCCESS_STATUS = "completed"
NO_CLASSIFIED_FAILURE = "no_classified_failure"
CODE_RECON_RE = re.compile(
    r"\b(rg|grep|find|ls|cat|sed|awk|head|tail|file|strings|nm|objdump|readelf|cflow)\b",
)
SECURITY_TOOL_RE = re.compile(
    r"\b(valgrind|klee|gdb|addr2line|cppcheck|clang-tidy|strace|ltrace|"
    r"afl-fuzz|honggfuzz|libfuzzer|asan_symbolize|asan_options|ubsan_options)\b",
    re.IGNORECASE,
)
CHEAT_PATTERN_FIELDS = (
    (
        "any_cheat",
        "Any cheat call",
        "Any shell tool-use event matching one of the cheat signatures below.",
        "cheat_tool_calls",
    ),
    (
        "git_log",
        "`git log`",
        "Reads commit history.",
        "cheat_git_log_calls",
    ),
    (
        "git_show",
        "`git show`",
        "Reads an object, commit, or historical file snapshot.",
        "cheat_git_show_calls",
    ),
    (
        "git_reflog",
        "`git reflog`",
        "Reads local ref movement history.",
        "cheat_git_reflog_calls",
    ),
    (
        "git_diff_history_ref",
        "`git diff <history/ref>`",
        "Diffs against a history-bearing ref such as `HEAD~1`, SHA, `A..B`, `refs/...`, or `@{1}`.",
        "cheat_git_diff_history_ref_calls",
    ),
    (
        "git_diff_unparsed",
        "`git diff` parse fallback",
        "Malformed shell quoting prevented parsing, but the command began with `git diff`.",
        "cheat_git_diff_unparsed_calls",
    ),
)
WORK_DIR_RE = re.compile(r"- Work Dir:\s*(`?)(/src/[^\s`]+)\1")
VALIDATION_KEY_RE = re.compile(r"^([A-Z0-9_]+):\s*(.*)$")
VALIDATION_TOKEN_RE = re.compile(r"[a-z0-9]+")
CRASH_IDENTIFIER_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*\b"
)
COST_ABS_TOLERANCE_USD = 1e-6
COST_REL_TOLERANCE = 1e-9
DISCOVER_STUDY_RUNS_SQL = """
WITH grouped AS (
    SELECT
        aggregate_id,
        max(payload->'config'->>'tool') FILTER (
            WHERE event_type = 'AgentCreated'
              AND payload->>'role' = 'boss'
        ) AS root_tool,
        max(payload->'config'->'base'->>'model') FILTER (
            WHERE event_type = 'AgentCreated'
              AND payload->>'role' = 'boss'
        ) AS root_model,
        max(payload->>'task_description') FILTER (
            WHERE event_type = 'TaskAssigned'
        ) AS task,
        max(payload->'domain_metadata'->>'instance_id') FILTER (
            WHERE event_type = 'RunStarted'
        ) AS instance_id,
        max(occurred_at) FILTER (
            WHERE event_type = 'RunStarted'
        ) AS started_at,
        bool_or(coalesce(payload->>'prompt', '') LIKE '%<task>%') FILTER (
            WHERE event_type = 'PromptSent'
        ) AS has_task_prompt,
        bool_or(coalesce(payload->>'prompt', '') LIKE '%' || $4 || '%') FILTER (
            WHERE event_type = 'PromptSent'
        ) AS has_a1_marker
    FROM events
    WHERE event_type IN ('AgentCreated', 'TaskAssigned', 'RunStarted', 'PromptSent')
    GROUP BY aggregate_id
)
SELECT
    aggregate_id::text AS run_id,
    CASE WHEN coalesce(has_a1_marker, false) THEN 'A1' ELSE 'A2' END AS cell,
    task,
    started_at
FROM grouped
WHERE root_tool = $1
  AND root_model = $2
  AND task = ANY($3::text[])
  AND task = instance_id
  AND started_at IS NOT NULL
  AND coalesce(has_task_prompt, false)
ORDER BY started_at, run_id
"""
INVALID_VALIDATION_VALUES = frozenset(
    {
        "",
        "unknown",
        "none",
        "n/a",
        "na",
        "not reproduced",
        "no crash",
        "no crash observed",
        "no file-based crash triggered",
    }
)
CRASH_IDENTIFIER_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "c",
        "called",
        "caller",
        "calls",
        "class",
        "const",
        "cpp",
        "current",
        "detected",
        "error",
        "file",
        "function",
        "h",
        "handling",
        "hpp",
        "in",
        "indirectly",
        "instance",
        "line",
        "logic",
        "macro",
        "main",
        "minimal_trigger",
        "no",
        "not",
        "observed",
        "or",
        "path",
        "precision",
        "program",
        "reproduced",
        "replicates",
        "related",
        "simulates",
        "src",
        "testcase",
        "timeout",
        "third",
        "trigger",
        "triggered",
        "via",
        "which",
    }
)


@dataclass(frozen=True)
class EnrollmentEntry:
    run_id: str
    cell: str
    task: str
    replicate: int
    started_at: str = ""


@dataclass(frozen=True)
class RunFiles:
    manifest_path: Path | None
    events_path: Path


@dataclass(frozen=True)
class StudyDiscoverySpec:
    study_id: str
    tasks: frozenset[str]
    root_tool: str
    root_model: str
    a1_prompt_marker: str


@dataclass(frozen=True)
class StudyRunCandidate:
    run_id: str
    cell: str
    task: str
    started_at: str


@dataclass(frozen=True)
class StudyRunDiscovery:
    entries: list[EnrollmentEntry]
    files_by_run_id: dict[str, RunFiles]
    db_candidate_run_ids: list[str]
    db_candidates_missing_runfiles: list[str]
    local_runfiles_missing_db_candidates: list[str]
    dataset_task_count: int


@dataclass(frozen=True)
class ToolAuditMetrics:
    actual_tool_calls: int
    cheat_tool_calls: int
    cheat_git_log_calls: int
    cheat_git_show_calls: int
    cheat_git_reflog_calls: int
    cheat_git_diff_history_ref_calls: int
    cheat_git_diff_unparsed_calls: int
    recon_tool_calls: int
    security_tool_calls: int
    subagent_tool_calls: int
    task_family_tool_calls: int
    task_create_tool_calls: int
    task_update_tool_calls: int
    task_list_tool_calls: int
    file_read_tool_calls: int
    file_write_tool_calls: int
    file_edit_tool_calls: int
    search_tool_calls: int
    shell_tool_calls: int
    task_mgmt_tool_calls: int
    web_forbidden_tool_calls: int
    mcp_tool_calls: int
    other_tool_calls: int
    bash_recon_tool_calls: int
    bash_exploit_tool_calls: int
    bash_security_scan_tool_calls: int
    bash_build_tool_calls: int
    bash_test_exec_tool_calls: int
    bash_git_tool_calls: int
    bash_other_shell_tool_calls: int


@dataclass(frozen=True)
class ResultEvidence:
    builder_real_success: int
    builder_evidence_present: int
    builder_executable_count: int
    builder_executables: str
    builder_work_bin_executable_count: int
    builder_work_bin_executables: str
    builder_src_executable_count: int
    builder_src_executables: str
    exploit_validation_present: int
    exploit_verdict_pass: int
    exploit_determinism_pass: int
    exploit_error_match: int
    exploit_crash_function_match: int
    exploiter_real_success: int
    exploit_expected_error: str
    exploit_observed_error: str
    exploit_expected_crash_function: str
    exploit_observed_crash_function: str
    exploit_determinism_runs: str
    patch_validation_present: int
    patch_verdict_pass: int
    patch_apply_clean: int
    patch_build_success: int
    patch_post_error_none: int
    patch_repro_runs_no_crash: int
    fixer_real_success: int
    patch_post_error: str
    patch_repro_runs: str
    patched_files: str
    model_patch_present: int
    repro_script_present: int
    security_report_present: int
    real_pipeline_success: int


@dataclass(frozen=True)
class RunRow:
    source_authority: str
    run_id: str
    cell: str
    task: str
    replicate: int
    started_at: str
    db_event_count: int
    runs_event_count: int
    db_runs_event_ids_match: int
    db_runs_event_records_match: int
    db_runs_cost_match: int
    terminal: int
    successful: int
    run_status: str
    failure_mode: str
    failure_reason: str
    auth_failure: int
    environmental_failure: int
    environmental_failure_cause: str
    manager_cost_usd: float
    worker_cost_usd: float
    total_cost_usd: float
    has_observed_cost: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    run_duration_seconds: float | None
    wall_clock_seconds: float | None
    total_tool_calls: int
    bash_tool_calls: int
    task_mgmt_tool_calls: int
    task_tool_calls: int
    forbidden_web_attempts: int
    worker_cost_event_count: int
    aggregate_count: int
    actual_tool_calls: int = 0
    cheat_tool_calls: int = 0
    cheat_git_log_calls: int = 0
    cheat_git_show_calls: int = 0
    cheat_git_reflog_calls: int = 0
    cheat_git_diff_history_ref_calls: int = 0
    cheat_git_diff_unparsed_calls: int = 0
    recon_tool_calls: int = 0
    security_tool_calls: int = 0
    subagent_tool_calls: int = 0
    task_family_tool_calls: int = 0
    task_create_tool_calls: int = 0
    task_update_tool_calls: int = 0
    task_list_tool_calls: int = 0
    file_read_tool_calls: int = 0
    file_write_tool_calls: int = 0
    file_edit_tool_calls: int = 0
    search_tool_calls: int = 0
    shell_tool_calls: int = 0
    web_forbidden_tool_calls: int = 0
    mcp_tool_calls: int = 0
    other_tool_calls: int = 0
    bash_recon_tool_calls: int = 0
    bash_exploit_tool_calls: int = 0
    bash_security_scan_tool_calls: int = 0
    bash_build_tool_calls: int = 0
    bash_test_exec_tool_calls: int = 0
    bash_git_tool_calls: int = 0
    bash_other_shell_tool_calls: int = 0
    builder_real_success: int = 0
    builder_evidence_present: int = 0
    builder_executable_count: int = 0
    builder_executables: str = ""
    builder_work_bin_executable_count: int = 0
    builder_work_bin_executables: str = ""
    builder_src_executable_count: int = 0
    builder_src_executables: str = ""
    exploit_validation_present: int = 0
    exploit_verdict_pass: int = 0
    exploit_determinism_pass: int = 0
    exploit_error_match: int = 0
    exploit_crash_function_match: int = 0
    exploiter_real_success: int = 0
    exploit_expected_error: str = ""
    exploit_observed_error: str = ""
    exploit_expected_crash_function: str = ""
    exploit_observed_crash_function: str = ""
    exploit_determinism_runs: str = ""
    patch_validation_present: int = 0
    patch_verdict_pass: int = 0
    patch_apply_clean: int = 0
    patch_build_success: int = 0
    patch_post_error_none: int = 0
    patch_repro_runs_no_crash: int = 0
    fixer_real_success: int = 0
    patch_post_error: str = ""
    patch_repro_runs: str = ""
    patched_files: str = ""
    model_patch_present: int = 0
    repro_script_present: int = 0
    security_report_present: int = 0
    real_pipeline_success: int = 0


@dataclass(frozen=True)
class PairRow:
    task: str
    replicate: int
    a1_run_id: str
    a2_run_id: str
    pair_included: int
    a1_successful: int
    a2_successful: int
    a1_environmental_failure: int
    a2_environmental_failure: int
    success_delta_a1_minus_a2: int
    total_cost_delta_a1_minus_a2: float | None
    duration_delta_a1_minus_a2: float | None
    total_tokens_delta_a1_minus_a2: int | None
    tool_call_delta_a1_minus_a2: int
    task_mgmt_tool_delta_a1_minus_a2: int
    task_tool_delta_a1_minus_a2: int
    a1_failure_mode: str
    a2_failure_mode: str
    a1_total_cost_usd: float | None = None
    a2_total_cost_usd: float | None = None
    a1_duration_seconds: float | None = None
    a2_duration_seconds: float | None = None
    a1_total_tokens: int | None = None
    a2_total_tokens: int | None = None
    a1_total_tool_calls: int = 0
    a2_total_tool_calls: int = 0
    a1_actual_tool_calls: int = 0
    a2_actual_tool_calls: int = 0
    actual_tool_delta_a1_minus_a2: int = 0
    a1_task_mgmt_tool_calls: int = 0
    a2_task_mgmt_tool_calls: int = 0
    a1_task_tool_calls: int = 0
    a2_task_tool_calls: int = 0
    task_family_tool_delta_a1_minus_a2: int = 0
    a1_task_family_tool_calls: int = 0
    a2_task_family_tool_calls: int = 0
    task_create_tool_delta_a1_minus_a2: int = 0
    a1_task_create_tool_calls: int = 0
    a2_task_create_tool_calls: int = 0
    task_update_tool_delta_a1_minus_a2: int = 0
    a1_task_update_tool_calls: int = 0
    a2_task_update_tool_calls: int = 0
    task_list_tool_delta_a1_minus_a2: int = 0
    a1_task_list_tool_calls: int = 0
    a2_task_list_tool_calls: int = 0
    a1_builder_real_success: int = 0
    a2_builder_real_success: int = 0
    builder_success_delta_a1_minus_a2: int = 0
    a1_exploiter_real_success: int = 0
    a2_exploiter_real_success: int = 0
    exploiter_success_delta_a1_minus_a2: int = 0
    a1_fixer_real_success: int = 0
    a2_fixer_real_success: int = 0
    fixer_success_delta_a1_minus_a2: int = 0
    a1_real_pipeline_success: int = 0
    a2_real_pipeline_success: int = 0
    real_pipeline_success_delta_a1_minus_a2: int = 0
    cheat_tool_delta_a1_minus_a2: int = 0
    a1_cheat_tool_calls: int = 0
    a2_cheat_tool_calls: int = 0
    recon_tool_delta_a1_minus_a2: int = 0
    a1_recon_tool_calls: int = 0
    a2_recon_tool_calls: int = 0
    security_tool_delta_a1_minus_a2: int = 0
    a1_security_tool_calls: int = 0
    a2_security_tool_calls: int = 0


@dataclass(frozen=True)
class CellSummary:
    cell: str
    n: int
    terminal: int
    nonterminal: int
    successful: int
    success_rate: float
    success_rate_ci_low: float
    success_rate_ci_high: float
    terminal_success_rate: float
    terminal_success_rate_ci_low: float
    terminal_success_rate_ci_high: float
    auth_failures: int
    environmental_failures: int
    auth_login_failures: int
    credit_quota_failures: int
    provider_api_failures: int
    total_cost_usd: float
    manager_cost_usd: float
    worker_cost_usd: float
    mean_cost_usd: float | None
    median_cost_usd: float | None
    cost_per_success_usd: float | None
    cost_observed_runs: int
    positive_cost_runs: int
    zero_cost_event_runs: int
    missing_cost_runs: int
    total_tokens: int
    mean_tokens: float | None
    median_duration_seconds: float | None
    total_tool_calls: int
    mean_tool_calls: float | None
    task_mgmt_tool_calls: int
    task_tool_calls: int
    forbidden_web_attempts: int
    actual_tool_calls: int = 0
    cheat_tool_calls: int = 0
    cheat_git_log_calls: int = 0
    cheat_git_show_calls: int = 0
    cheat_git_reflog_calls: int = 0
    cheat_git_diff_history_ref_calls: int = 0
    cheat_git_diff_unparsed_calls: int = 0
    recon_tool_calls: int = 0
    security_tool_calls: int = 0
    subagent_tool_calls: int = 0
    task_family_tool_calls: int = 0
    task_create_tool_calls: int = 0
    task_update_tool_calls: int = 0
    task_list_tool_calls: int = 0
    file_read_tool_calls: int = 0
    file_write_tool_calls: int = 0
    file_edit_tool_calls: int = 0
    search_tool_calls: int = 0
    shell_tool_calls: int = 0
    web_forbidden_tool_calls: int = 0
    mcp_tool_calls: int = 0
    other_tool_calls: int = 0
    bash_recon_tool_calls: int = 0
    bash_exploit_tool_calls: int = 0
    bash_security_scan_tool_calls: int = 0
    bash_build_tool_calls: int = 0
    bash_test_exec_tool_calls: int = 0
    bash_git_tool_calls: int = 0
    bash_other_shell_tool_calls: int = 0
    builder_real_successes: int = 0
    builder_evidence_present: int = 0
    builder_executable_runs: int = 0
    builder_executable_count: int = 0
    exploiter_real_successes: int = 0
    exploit_validation_present: int = 0
    exploit_verdict_pass: int = 0
    exploit_error_match: int = 0
    exploit_crash_function_match: int = 0
    fixer_real_successes: int = 0
    patch_validation_present: int = 0
    patch_verdict_pass: int = 0
    patch_apply_clean: int = 0
    patch_build_success: int = 0
    patch_post_error_none: int = 0
    real_pipeline_successes: int = 0
    model_patch_present: int = 0
    repro_script_present: int = 0
    security_report_present: int = 0


def _repo_path(*parts: str) -> Path:
    return get_repo_root().joinpath(*parts)


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _enrollment_lock_path(study_id: str) -> Path:
    return _repo_path("experiments", study_id, "reports", "enrollment.lock.yaml")


def _runs_root() -> Path:
    return _repo_path("runs")


def _load_enrollment_items(study_id: str) -> list[dict[str, Any]]:
    path = _enrollment_lock_path(study_id)
    lock = _load_yaml(path)
    enrollment = lock.get("enrollment", [])
    if not isinstance(enrollment, list):
        raise ValueError(f"enrollment lock has non-list enrollment: {path}")
    items: list[dict[str, Any]] = []
    for item in enrollment:
        if not isinstance(item, dict):
            raise ValueError(f"enrollment lock has non-object entries: {path}")
        items.append(item)
    return items


def _load_study_tasks(study_id: str) -> frozenset[str]:
    dataset_path = _repo_path("experiments", study_id, "dataset.yaml")
    dataset = _load_yaml(dataset_path)
    tasks = dataset.get("default_cves")
    if not isinstance(tasks, list) or not all(isinstance(task, str) for task in tasks):
        raise ValueError(f"dataset missing string default_cves list: {dataset_path}")
    return frozenset(tasks)


def _study_discovery_spec(study_id: str) -> StudyDiscoverySpec:
    if study_id != STUDY_ID:
        raise ValueError(f"DB+runs discovery is not configured for study_id={study_id!r}")
    return StudyDiscoverySpec(
        study_id=study_id,
        tasks=_load_study_tasks(study_id),
        root_tool="claude_code",
        root_model="claude-opus-4-5-20251101",
        a1_prompt_marker="Task subagent tool is available",
    )


async def load_study_run_ids(study_id: str) -> list[str]:
    """Return ordered run IDs discovered from DB root events and top-level runs files."""
    conn = await open_connection()
    try:
        discovery = await discover_study_runs(conn, study_id)
    finally:
        await conn.close()
    return [entry.run_id for entry in discovery.entries]


def load_lock_run_ids(study_id: str) -> list[str]:
    """Return ordered run IDs explicitly enrolled in a study lockfile."""
    run_ids: list[str] = []
    seen: set[str] = set()
    for item in _load_enrollment_items(study_id):
        run_id = item.get("run_id")
        if not isinstance(run_id, str):
            raise ValueError(f"enrollment lock entry missing string run_id: {study_id}")
        if run_id in seen:
            raise ValueError(f"duplicate run_id in enrollment lock {study_id}: {run_id}")
        seen.add(run_id)
        run_ids.append(run_id)
    return run_ids


def load_enrollment(study_id: str = STUDY_ID) -> list[EnrollmentEntry]:
    """Load A1/A2 entries from the immutable enrollment lockfile."""
    entries = []
    for item in _load_enrollment_items(study_id):
        cell = item.get("cell")
        if cell not in {"A1", "A2"}:
            continue
        entries.append(
            EnrollmentEntry(
                run_id=str(item["run_id"]),
                cell=str(cell),
                task=str(item["task"]),
                replicate=int(item["replicate"]),
            )
        )
    return entries


def _load_lock_entries_for_audit(study_id: str) -> tuple[list[EnrollmentEntry], str]:
    try:
        return load_enrollment(study_id), ""
    except (OSError, yaml.YAMLError, ValueError, KeyError, TypeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _entry_from_manifest(manifest: dict[str, Any], manifest_path: Path) -> EnrollmentEntry:
    run_id = manifest.get("run_id")
    cell = manifest.get("cell")
    task = manifest.get("task")
    replicate = manifest.get("replicate")
    if not isinstance(run_id, str):
        raise ValueError(f"run manifest missing string run_id: {manifest_path}")
    if cell not in {"A1", "A2"}:
        raise ValueError(f"run manifest has unexpected A1/A2 cell: {manifest_path}")
    if not isinstance(task, str):
        raise ValueError(f"run manifest missing string task: {manifest_path}")
    if not isinstance(replicate, int):
        raise ValueError(f"run manifest missing integer replicate: {manifest_path}")
    return EnrollmentEntry(
        run_id=run_id,
        cell=str(cell),
        task=task,
        replicate=replicate,
    )


def load_runfile_entries(
    study_id: str = STUDY_ID,
    pool_roots: list[Path] | None = None,
) -> tuple[list[EnrollmentEntry], dict[str, RunFiles]]:
    """Load the A1/A2 source cohort directly from actual run directories."""
    entries: list[EnrollmentEntry] = []
    files_by_run_id: dict[str, RunFiles] = {}
    for manifest_path in iter_run_manifests(pool_roots=pool_roots):
        manifest = read_run_manifest(manifest_path)
        if (
            manifest.get("study_id") != study_id
            or manifest.get("cell") not in {"A1", "A2"}
        ):
            continue

        entry = _entry_from_manifest(manifest, manifest_path)
        events_path = manifest_path.parent / "events.jsonl"
        if not events_path.is_file():
            raise FileNotFoundError(f"events file missing for run {entry.run_id}")

        files = RunFiles(manifest_path=manifest_path, events_path=events_path)
        existing = files_by_run_id.get(entry.run_id)
        if existing is not None and existing != files:
            raise ValueError(f"run_id {entry.run_id} appears in multiple pool roots")
        if existing is None:
            entries.append(entry)
            files_by_run_id[entry.run_id] = files

    entries.sort(key=lambda entry: (entry.cell, entry.task, entry.replicate, entry.run_id))
    return entries, files_by_run_id


async def discover_study_runs(
    conn: asyncpg.Connection,
    study_id: str,
    *,
    runs_root: Path | None = None,
) -> StudyRunDiscovery:
    spec = _study_discovery_spec(study_id)
    candidates = await _fetch_study_run_candidates(conn, spec)
    event_files = _discover_run_event_files(runs_root or _runs_root())
    return _build_discovery_from_candidates(
        candidates=candidates,
        event_files=event_files,
        dataset_task_count=len(spec.tasks),
    )


async def _fetch_study_run_candidates(
    conn: asyncpg.Connection,
    spec: StudyDiscoverySpec,
) -> list[StudyRunCandidate]:
    rows = await conn.fetch(
        DISCOVER_STUDY_RUNS_SQL,
        spec.root_tool,
        spec.root_model,
        sorted(spec.tasks),
        spec.a1_prompt_marker,
    )
    candidates: list[StudyRunCandidate] = []
    for row in rows:
        candidates.append(
            StudyRunCandidate(
                run_id=str(row["run_id"]),
                cell=str(row["cell"]),
                task=str(row["task"]),
                started_at=row["started_at"].isoformat().replace("+00:00", "Z"),
            )
        )
    return candidates


def _discover_run_event_files(runs_root: Path) -> dict[str, Path]:
    if not runs_root.is_dir():
        return {}
    event_files: dict[str, Path] = {}
    for events_path in runs_root.glob("*/events.jsonl"):
        run_id = events_path.parent.name
        if run_id in event_files and event_files[run_id] != events_path:
            raise ValueError(f"run_id {run_id} has duplicate events files under {runs_root}")
        event_files[run_id] = events_path
    return event_files


def _build_discovery_from_candidates(
    *,
    candidates: list[StudyRunCandidate],
    event_files: dict[str, Path],
    dataset_task_count: int,
) -> StudyRunDiscovery:
    candidate_by_run_id = {candidate.run_id: candidate for candidate in candidates}
    entries: list[EnrollmentEntry] = []
    files_by_run_id: dict[str, RunFiles] = {}
    for candidate in candidates:
        events_path = event_files.get(candidate.run_id)
        if events_path is None:
            continue
        entries.append(
            EnrollmentEntry(
                run_id=candidate.run_id,
                cell=candidate.cell,
                task=candidate.task,
                replicate=0,
                started_at=candidate.started_at,
            )
        )
        files_by_run_id[candidate.run_id] = RunFiles(
            manifest_path=None,
            events_path=events_path,
        )
    entries.sort(key=lambda entry: (entry.cell, entry.task, entry.replicate, entry.run_id))
    return StudyRunDiscovery(
        entries=entries,
        files_by_run_id=files_by_run_id,
        db_candidate_run_ids=[candidate.run_id for candidate in candidates],
        db_candidates_missing_runfiles=sorted(set(candidate_by_run_id) - set(event_files)),
        local_runfiles_missing_db_candidates=sorted(set(event_files) - set(candidate_by_run_id)),
        dataset_task_count=dataset_task_count,
    )


def _scope_entries_to_run_ids(
    source_entries: list[EnrollmentEntry],
    study_run_ids: list[str],
) -> tuple[list[EnrollmentEntry], list[str]]:
    study_run_id_set = set(study_run_ids)
    scoped = [entry for entry in source_entries if entry.run_id in study_run_id_set]
    out_of_lock = sorted(
        entry.run_id for entry in source_entries if entry.run_id not in study_run_id_set
    )
    return scoped, out_of_lock


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_cutoff(value: str) -> datetime:
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _format_cutoff_local(value: datetime) -> str:
    return value.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def read_run_events(path: Path) -> list[EventRow]:
    """Read projected local run events for one enrolled run."""
    events: list[EventRow] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            payload = {k: v for k, v in raw.items() if k not in EVENT_TOP_LEVEL_KEYS}
            events.append(
                EventRow(
                    event_id=UUID(raw["event_id"]),
                    aggregate_id=UUID(raw["aggregate_id"]),
                    sequence_number=int(raw["sequence_number"]),
                    event_type=str(raw["event_type"]),
                    payload=payload,
                    occurred_at=_parse_datetime(str(raw["occurred_at"])),
                    metadata=raw.get("metadata") or {},
                )
            )
    return events


def read_run_manifest(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError(f"run manifest is not a JSON object: {path}")
    return manifest


def _root_started_at(events: list[EventRow], run_id: str) -> str:
    run_uuid = UUID(run_id)
    for event in events:
        if event.aggregate_id == run_uuid and event.event_type == "RunStarted":
            return event.occurred_at.isoformat().replace("+00:00", "Z")
    for event in events:
        if event.aggregate_id == run_uuid and event.event_type == "AgentCreated":
            return event.occurred_at.isoformat().replace("+00:00", "Z")
    raise ValueError(f"no root start event found for run {run_id}")


def _manifest_started_at(files: RunFiles) -> datetime:
    if files.manifest_path is None:
        raise ValueError("run file has no manifest path")
    manifest = read_run_manifest(files.manifest_path)
    started_at = manifest.get("started_at")
    if not isinstance(started_at, str):
        raise ValueError(f"run manifest missing started_at: {files.manifest_path}")
    return _parse_datetime(started_at)


def filter_analysis_cohort(
    entries: list[EnrollmentEntry],
    files_by_run_id: dict[str, RunFiles],
    *,
    started_before: datetime,
) -> list[EnrollmentEntry]:
    """Freeze analysis to DB-discovered runs started before the configured cutoff."""
    del files_by_run_id
    return [entry for entry in entries if _parse_cutoff(entry.started_at) < started_before]


def _dedupe_latest_per_cell_task(
    entries: list[EnrollmentEntry],
) -> list[EnrollmentEntry]:
    """Keep one enrolled run per (cell, task) -- the latest started_at, run_id tie-break."""
    by_key: dict[tuple[str, str], EnrollmentEntry] = {}
    for entry in entries:
        key = (entry.cell, entry.task)
        existing = by_key.get(key)
        if existing is None or (
            _parse_cutoff(entry.started_at),
            entry.run_id,
        ) > (
            _parse_cutoff(existing.started_at),
            existing.run_id,
        ):
            by_key[key] = entry
    return sorted(
        by_key.values(),
        key=lambda entry: (entry.cell, entry.task, entry.replicate, entry.run_id),
    )


def resolve_run_files(
    entries: list[EnrollmentEntry],
    pool_roots: list[Path] | None = None,
) -> dict[str, RunFiles]:
    """Resolve locked run ids to their actual pool-root files."""
    wanted = {entry.run_id for entry in entries}
    resolved: dict[str, RunFiles] = {}
    for manifest_path in iter_run_manifests(pool_roots=pool_roots):
        manifest = read_run_manifest(manifest_path)
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or run_id not in wanted:
            continue
        events_path = manifest_path.parent / "events.jsonl"
        if not events_path.is_file():
            raise FileNotFoundError(f"events file missing for enrolled run {run_id}")
        existing = resolved.get(run_id)
        files = RunFiles(manifest_path=manifest_path, events_path=events_path)
        if existing is not None and existing != files:
            raise ValueError(f"run_id {run_id} appears in multiple pool roots")
        resolved[run_id] = files

    missing = sorted(wanted - set(resolved))
    if missing:
        raise FileNotFoundError(f"missing run files for enrolled run ids: {missing[:5]}")
    return resolved


def run_input_paths(
    entries: list[EnrollmentEntry],
    files_by_run_id: dict[str, RunFiles],
) -> list[str]:
    """Return local run files that make the DB-vs-runs check reproducible."""
    paths: list[str] = []
    for entry in entries:
        files = files_by_run_id[entry.run_id]
        if files.manifest_path is not None:
            paths.append(to_repo_relative(files.manifest_path))
        paths.append(to_repo_relative(files.events_path))
    return paths


def base_input_paths(study_id: str = STUDY_ID) -> list[str]:
    return [
        f"experiments/{study_id}/dataset.yaml",
        f"experiments/{study_id}/configs/A1-claude-code-subagent.yaml",
        f"experiments/{study_id}/configs/A2-claude-code-nosubagent.yaml",
    ]


def exact_two_sided_binomial_p(successes: int, trials: int) -> float:
    """Two-sided exact binomial p-value for p=0.5."""
    if trials <= 0:
        return 1.0
    tail = min(successes, trials - successes)
    probability = sum(math.comb(trials, i) for i in range(tail + 1)) / (2**trials)
    return min(1.0, 2 * probability)


def wilson_interval(successes: int, n: int) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return 0.0, 0.0
    z = 1.959963984540054
    proportion = successes / n
    denominator = 1 + z**2 / n
    center = (proportion + z**2 / (2 * n)) / denominator
    spread = z * math.sqrt((proportion * (1 - proportion) + z**2 / (4 * n)) / n)
    margin = spread / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def bootstrap_mean_ci(
    values: list[float],
    *,
    iterations: int = 5000,
    seed: int = 20260518,
) -> tuple[float | None, float | None]:
    """Deterministic percentile bootstrap CI for a mean."""
    if not values:
        return None, None
    rng = random.Random(seed)  # noqa: S311 - deterministic bootstrap, not security.
    n = len(values)
    samples = []
    for _ in range(iterations):
        samples.append(mean(values[rng.randrange(n)] for _ in range(n)))
    samples.sort()
    low_index = int(0.025 * (iterations - 1))
    high_index = int(0.975 * (iterations - 1))
    return samples[low_index], samples[high_index]


def _mean_or_none(values: list[float]) -> float | None:
    return mean(values) if values else None


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def _is_auth_failure(reason: str) -> bool:
    return "not logged in" in reason.lower() or "please run /login" in reason.lower()


def _environmental_failure_cause(reason: str, failure_mode: str) -> str:
    text = reason.lower()
    if _is_auth_failure(reason):
        return "auth_login"
    if "credit balance" in text or "quota" in text:
        return "credit_quota"
    if failure_mode == "provider_failure" or "rate limit" in text or "overload" in text:
        return "provider_api"
    return ""


def _assert_manifest_matches(entry: EnrollmentEntry, manifest: dict[str, Any]) -> None:
    expected = {
        "study_id": STUDY_ID,
        "run_id": entry.run_id,
        "cell": entry.cell,
        "task": entry.task,
        "replicate": entry.replicate,
    }
    for key, expected_value in expected.items():
        if manifest.get(key) != expected_value:
            raise ValueError(
                f"run manifest drift for {entry.run_id}: "
                f"{key}={manifest.get(key)!r}, expected {expected_value!r}"
            )


def _event_ids(events: list[EventRow]) -> list[str]:
    return [str(event.event_id) for event in events]


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event_record(event: EventRow) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id),
        "aggregate_id": str(event.aggregate_id),
        "sequence_number": event.sequence_number,
        "event_type": event.event_type,
        "occurred_at": _iso_datetime(event.occurred_at),
        "payload": _domain_payload(event.payload),
        "metadata": event.metadata,
    }


def _domain_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in EVENT_TOP_LEVEL_KEYS
    }


def event_records_match(db_events: list[EventRow], run_events: list[EventRow]) -> bool:
    """Return True only when DB and run-file events are identical records."""
    return [_event_record(event) for event in db_events] == [
        _event_record(event) for event in run_events
    ]


def _first_event_record_mismatch(
    db_events: list[EventRow],
    run_events: list[EventRow],
) -> str:
    for index, (db_event, run_event) in enumerate(
        zip(db_events, run_events, strict=False)
    ):
        if _event_record(db_event) != _event_record(run_event):
            return f"first_record_mismatch_index={index}"
    if len(db_events) != len(run_events):
        return f"event_count_mismatch db={len(db_events)} runs={len(run_events)}"
    return "record_mismatch"


def _costs_match(db_events: list[EventRow], run_events: list[EventRow]) -> bool:
    db_cost = compute_cost(db_events)
    run_cost = compute_cost(run_events)
    return math.isclose(
        db_cost.total_cost_usd,
        run_cost.total_cost_usd,
        rel_tol=COST_REL_TOLERANCE,
        abs_tol=COST_ABS_TOLERANCE_USD,
    )


def _worker_cost_event_count(events: list[EventRow]) -> int:
    return sum(event.event_type == "WorkerCostRecorded" for event in events)


def _iter_tool_use_events(events: list[EventRow]) -> list[tuple[EventRow, str, str]]:
    tool_events = []
    for event in events:
        if event.event_type != "ThoughtCaptured":
            continue
        payload = event.payload
        if payload.get("output_type") != "tool_use":
            continue
        content = payload.get("content") or ""
        tool_name = recover_tool_name(content, payload.get("tool_name"))
        tool_events.append((event, tool_name, content))
    return tool_events


def compute_tool_audit(events: list[EventRow], tools: Any) -> ToolAuditMetrics:
    """Compute cross-cutting tool counts requested by the result analysis."""
    by_category = tools.by_category
    by_tool_name = tools.by_tool_name
    bash_recon = 0
    bash_security = 0
    cheat_calls = 0
    cheat_patterns: Counter[str] = Counter()
    for _event, tool_name, content in _iter_tool_use_events(events):
        if classify_tool("A", tool_name) is not ToolCategory.SHELL:
            continue
        command = _extract_bash_command(content) or ""
        pattern = _cheating_pattern(command)
        if pattern is not None:
            cheat_calls += 1
            cheat_patterns[pattern] += 1
        if CODE_RECON_RE.search(command):
            bash_recon += 1
        if SECURITY_TOOL_RE.search(command.replace("-", "_")):
            bash_security += 1

    mcp_security_calls = sum(
        count
        for name, count in by_tool_name.items()
        if name == "security_tools" or name.startswith("mcp__security_tools")
    )
    file_read = by_category.get("file_read", 0)
    search = by_category.get("search", 0)
    subagent = by_category.get("subagent_spawn", 0)
    task_mgmt = by_category.get("task_mgmt", 0)
    return ToolAuditMetrics(
        actual_tool_calls=tools.total_tool_calls,
        cheat_tool_calls=cheat_calls,
        cheat_git_log_calls=cheat_patterns["git_log"],
        cheat_git_show_calls=cheat_patterns["git_show"],
        cheat_git_reflog_calls=cheat_patterns["git_reflog"],
        cheat_git_diff_history_ref_calls=cheat_patterns["git_diff_history_ref"],
        cheat_git_diff_unparsed_calls=cheat_patterns["git_diff_unparsed"],
        recon_tool_calls=file_read + search + bash_recon,
        security_tool_calls=mcp_security_calls + bash_security,
        subagent_tool_calls=subagent,
        task_family_tool_calls=task_mgmt + subagent,
        task_create_tool_calls=by_tool_name.get("TaskCreate", 0),
        task_update_tool_calls=by_tool_name.get("TaskUpdate", 0),
        task_list_tool_calls=by_tool_name.get("TaskList", 0),
        file_read_tool_calls=file_read,
        file_write_tool_calls=by_category.get("file_write", 0),
        file_edit_tool_calls=by_category.get("file_edit", 0),
        search_tool_calls=search,
        shell_tool_calls=by_category.get("shell", 0),
        task_mgmt_tool_calls=task_mgmt,
        web_forbidden_tool_calls=by_category.get("web_forbidden", 0),
        mcp_tool_calls=by_category.get("mcp", 0),
        other_tool_calls=by_category.get("other", 0),
        bash_recon_tool_calls=tools.bash_subtypes.get("recon", 0) + bash_recon,
        bash_exploit_tool_calls=tools.bash_subtypes.get("exploit", 0),
        bash_security_scan_tool_calls=tools.bash_subtypes.get("security_scan", 0)
        + bash_security,
        bash_build_tool_calls=tools.bash_subtypes.get("build", 0),
        bash_test_exec_tool_calls=tools.bash_subtypes.get("test_exec", 0),
        bash_git_tool_calls=tools.bash_subtypes.get("git", 0),
        bash_other_shell_tool_calls=tools.bash_subtypes.get("other_shell", 0),
    )


def _read_text_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _parse_validation_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = _read_text_if_present(path)
    if not text:
        return values
    for line in text.splitlines():
        match = VALIDATION_KEY_RE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def _is_pass(value: str) -> bool:
    return value.strip().upper() == "PASS"


def _is_clean(value: str) -> bool:
    return value.strip().lower() == "clean"


def _is_success(value: str) -> bool:
    return value.strip().lower() == "success"


def _is_none_value(value: str) -> bool:
    return value.strip().lower() in {"none", "no", "no sanitizer error", "no errors"}


def _runs_all_pass(value: str, *, expected_runs: int = 3) -> bool:
    match = re.search(r"(\d+)\s*/\s*(\d+)", value)
    if not match:
        return False
    passed = int(match.group(1))
    total = int(match.group(2))
    return passed == total and total >= expected_runs


def _invalid_validation_value(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in INVALID_VALIDATION_VALUES or normalized.startswith(
        ("none ", "n/a ", "not reproduced", "no crash", "no file-based crash")
    )


def _validation_tokens(value: str) -> set[str]:
    return set(VALIDATION_TOKEN_RE.findall(value.lower()))


def _sanitizer_error_matches(expected: str, observed: str) -> bool:
    if _invalid_validation_value(expected) or _invalid_validation_value(observed):
        return False
    expected_tokens = _validation_tokens(expected)
    observed_tokens = _validation_tokens(observed)
    return bool(
        expected_tokens
        and observed_tokens
        and (expected_tokens <= observed_tokens or observed_tokens <= expected_tokens)
    )


def _normalize_identifier(value: str) -> str:
    return value.lower()


def _crash_identifiers(value: str) -> set[str]:
    identifiers: set[str] = set()
    for match in CRASH_IDENTIFIER_RE.finditer(value):
        identifier = _normalize_identifier(match.group(0))
        if (
            identifier in CRASH_IDENTIFIER_STOPWORDS
            or identifier.isdigit()
            or len(identifier) == 1
        ):
            continue
        identifiers.add(identifier)
    return identifiers


def _identifier_leaf(identifier: str) -> str:
    return identifier.rsplit("::", maxsplit=1)[-1]


def _crash_function_matches(expected: str, observed: str) -> bool:
    if _invalid_validation_value(expected) or _invalid_validation_value(observed):
        return False
    expected_identifiers = _crash_identifiers(expected)
    observed_identifiers = _crash_identifiers(observed)
    if not expected_identifiers or not observed_identifiers:
        return False
    if expected_identifiers & observed_identifiers:
        return True
    expected_leaves = {_identifier_leaf(identifier) for identifier in expected_identifiers}
    observed_leaves = {_identifier_leaf(identifier) for identifier in observed_identifiers}
    return bool(expected_leaves & observed_leaves)


def _real_pipeline_success(
    *,
    terminal: bool,
    successful: bool,
    evidence: ResultEvidence,
) -> int:
    return int(
        terminal
        and successful
        and evidence.builder_real_success
        and evidence.exploiter_real_success
        and evidence.fixer_real_success
    )


def _extract_work_dir(events: list[EventRow]) -> str:
    for event in events:
        if event.event_type != "PromptSent":
            continue
        prompt = event.payload.get("prompt")
        if not isinstance(prompt, str):
            continue
        match = WORK_DIR_RE.search(prompt)
        if match:
            return match.group(2)
    return ""


def _prompt_excerpt(prompt: str, *, focus: str = "") -> str:
    if len(prompt) <= PROMPT_SAMPLE_CHAR_LIMIT:
        return prompt

    if focus and focus in prompt:
        head_limit = PROMPT_SAMPLE_CHAR_LIMIT // 2
        focus_limit = PROMPT_SAMPLE_CHAR_LIMIT - head_limit
        focus_index = prompt.index(focus)
        focus_start = max(0, focus_index - focus_limit // 3)
        focus_end = min(len(prompt), focus_start + focus_limit)
        focus_start = max(0, focus_end - focus_limit)
        omitted = focus_start - head_limit
        if omitted > 0:
            return (
                f"{prompt[:head_limit].rstrip()}\n\n"
                f"... [omitted {omitted:,} chars] ...\n\n"
                f"{prompt[focus_start:focus_end].strip()}"
            )

    head_limit = PROMPT_SAMPLE_CHAR_LIMIT // 2
    tail_limit = PROMPT_SAMPLE_CHAR_LIMIT - head_limit
    omitted = len(prompt) - head_limit - tail_limit
    return (
        f"{prompt[:head_limit].rstrip()}\n\n"
        f"... [omitted {omitted:,} chars] ...\n\n"
        f"{prompt[-tail_limit:].lstrip()}"
    )


def _prompt_samples(
    entries: list[EnrollmentEntry],
    files_by_run_id: dict[str, RunFiles],
    *,
    cells: tuple[str, ...] = ("A1", "A2"),
) -> dict[str, dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}
    sorted_entries = sorted(
        entries,
        key=lambda entry: (entry.cell, entry.task, entry.started_at, entry.run_id),
    )
    for cell in cells:
        for entry in sorted_entries:
            if entry.cell != cell:
                continue
            events = read_run_events(files_by_run_id[entry.run_id].events_path)
            for event in events:
                if event.event_type != "PromptSent":
                    continue
                prompt = event.payload.get("prompt")
                if not isinstance(prompt, str):
                    continue
                samples[cell] = {
                    "run_id": entry.run_id,
                    "run_id_short": entry.run_id[:8],
                    "task": entry.task,
                    "started_at": entry.started_at,
                    "prompt_chars": len(prompt),
                    "has_task_subagent_note": "Task subagent tool is available" in prompt,
                    "excerpt": _prompt_excerpt(
                        prompt,
                        focus="Task subagent tool is available",
                    ),
                }
                break
            if cell in samples:
                break
    return samples


def _mapped_src_work_dir(run_dir: Path, work_dir: str) -> Path | None:
    if not work_dir.startswith("/src/"):
        return None
    return run_dir / "src" / work_dir.removeprefix("/src/")


def _is_executable_file(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return path.is_file() and bool(mode & 0o111)


def _relative_executables(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    paths = []
    for path in root.rglob("*"):
        if _is_executable_file(path):
            paths.append(str(path.relative_to(root)))
    return sorted(paths)


def _join_evidence(items: list[str]) -> str:
    if not items:
        return ""
    shown = items[:MAX_EVIDENCE_LIST_ITEMS]
    suffix = "" if len(items) <= MAX_EVIDENCE_LIST_ITEMS else f"; +{len(items) - len(shown)} more"
    return "; ".join(shown) + suffix


def compute_result_evidence(run_dir: Path, events: list[EventRow]) -> ResultEvidence:
    """Derive builder/exploiter/fixer real-success evidence from run artifacts."""
    testcase_dir = run_dir / "testcase"
    work_bin_executables = _relative_executables(run_dir / "work" / "bin")
    work_dir = _extract_work_dir(events)
    src_work_dir = _mapped_src_work_dir(run_dir, work_dir)
    src_executables = _relative_executables(src_work_dir) if src_work_dir else []
    executable_count = len(work_bin_executables)
    builder_success = int(executable_count > 0)

    exploit_path = testcase_dir / "exploit_validation_results.txt"
    exploit = _parse_validation_file(exploit_path)
    exploit_present = int(bool(exploit))
    exploit_verdict_pass = int(_is_pass(exploit.get("VERDICT", "")))
    exploit_determinism_pass = int(_runs_all_pass(exploit.get("DETERMINISM_RUNS", "")))
    exploit_error_match = int(
        _sanitizer_error_matches(
            exploit.get("EXPECTED_SANITIZER_ERROR", ""),
            exploit.get("OBSERVED_SANITIZER_ERROR", ""),
        )
    )
    exploit_crash_match = int(
        _crash_function_matches(
            exploit.get("CRASH_FUNCTION_EXPECTED", ""),
            exploit.get("CRASH_FUNCTION_OBSERVED", ""),
        )
    )
    exploiter_success = int(
        exploit_present
        and exploit_verdict_pass
        and exploit_determinism_pass
        and exploit_error_match
        and exploit_crash_match
    )

    patch_path = testcase_dir / "patch_validation_results.txt"
    patch = _parse_validation_file(patch_path)
    patch_present = int(bool(patch))
    patch_verdict_pass = int(_is_pass(patch.get("VERDICT", "")))
    patch_apply_clean = int(_is_clean(patch.get("PATCH_APPLY_STATUS", "")))
    patch_build_success = int(_is_success(patch.get("BUILD_STATUS", "")))
    patch_post_error_none = int(_is_none_value(patch.get("POST_PATCH_SANITIZER_ERROR", "")))
    patch_repro_no_crash = int(_runs_all_pass(patch.get("REPRO_RUNS_NO_CRASH", "")))
    fixer_success = int(
        patch_present
        and patch_verdict_pass
        and patch_apply_clean
        and patch_build_success
        and patch_post_error_none
        and patch_repro_no_crash
    )

    model_patch_present = int((testcase_dir / "model_patch.diff").is_file())
    repro_present = int((testcase_dir / "repro.sh").is_file())
    security_report_present = int((testcase_dir / "security_report.md").is_file())
    return ResultEvidence(
        builder_real_success=builder_success,
        builder_evidence_present=int(executable_count > 0),
        builder_executable_count=executable_count,
        builder_executables=_join_evidence(work_bin_executables),
        builder_work_bin_executable_count=len(work_bin_executables),
        builder_work_bin_executables=_join_evidence(work_bin_executables),
        builder_src_executable_count=len(src_executables),
        builder_src_executables=_join_evidence(src_executables),
        exploit_validation_present=exploit_present,
        exploit_verdict_pass=exploit_verdict_pass,
        exploit_determinism_pass=exploit_determinism_pass,
        exploit_error_match=exploit_error_match,
        exploit_crash_function_match=exploit_crash_match,
        exploiter_real_success=exploiter_success,
        exploit_expected_error=exploit.get("EXPECTED_SANITIZER_ERROR", ""),
        exploit_observed_error=exploit.get("OBSERVED_SANITIZER_ERROR", ""),
        exploit_expected_crash_function=exploit.get("CRASH_FUNCTION_EXPECTED", ""),
        exploit_observed_crash_function=exploit.get("CRASH_FUNCTION_OBSERVED", ""),
        exploit_determinism_runs=exploit.get("DETERMINISM_RUNS", ""),
        patch_validation_present=patch_present,
        patch_verdict_pass=patch_verdict_pass,
        patch_apply_clean=patch_apply_clean,
        patch_build_success=patch_build_success,
        patch_post_error_none=patch_post_error_none,
        patch_repro_runs_no_crash=patch_repro_no_crash,
        fixer_real_success=fixer_success,
        patch_post_error=patch.get("POST_PATCH_SANITIZER_ERROR", ""),
        patch_repro_runs=patch.get("REPRO_RUNS_NO_CRASH", ""),
        patched_files=patch.get("PATCHED_FILES", ""),
        model_patch_present=model_patch_present,
        repro_script_present=repro_present,
        security_report_present=security_report_present,
        real_pipeline_success=int(builder_success and exploiter_success and fixer_success),
    )


async def build_run_row(
    conn: asyncpg.Connection,
    entry: EnrollmentEntry,
    files: RunFiles,
) -> RunRow:
    """Compute one source-of-truth row and assert DB/runs agreement."""
    db_events = await fetch_run_events(conn, UUID(entry.run_id))
    run_events = read_run_events(files.events_path)
    if not db_events:
        raise ValueError(f"no DB events for enrolled run {entry.run_id}")

    event_ids_match = _event_ids(db_events) == _event_ids(run_events)
    records_match = event_records_match(db_events, run_events)
    cost_match = _costs_match(db_events, run_events)
    if not event_ids_match or not records_match or not cost_match:
        raise ValueError(
            f"DB/runs disagreement for {entry.run_id}: "
            f"event_ids_match={event_ids_match}, "
            f"event_records_match={records_match}, "
            f"cost_match={cost_match}, "
            f"{_first_event_record_mismatch(db_events, run_events)}"
        )

    result = await compute_run_result(conn, UUID(entry.run_id), family="A")
    quantitative = result.quantitative
    outcomes = quantitative.outcomes
    cost = quantitative.cost
    tools = quantitative.tools
    timing = quantitative.timing
    tool_audit = compute_tool_audit(db_events, tools)
    result_evidence = compute_result_evidence(files.events_path.parent, db_events)
    failure_reason = outcomes.failure_reason or ""
    failure_mode = outcomes.failure_mode or ""
    environmental_cause = _environmental_failure_cause(failure_reason, failure_mode)
    successful = (
        outcomes.has_run_completed
        and outcomes.has_work_completed
        and outcomes.run_status == SUCCESS_STATUS
    )
    real_pipeline_success = _real_pipeline_success(
        terminal=outcomes.has_run_completed,
        successful=successful,
        evidence=result_evidence,
    )
    worker_cost_event_count = _worker_cost_event_count(db_events)
    has_observed_cost = worker_cost_event_count > 0 or cost.manager_cost_usd > 0.0
    started_at = entry.started_at or _root_started_at(db_events, entry.run_id)

    return RunRow(
        source_authority="db_events_verified_against_runs",
        run_id=entry.run_id,
        cell=entry.cell,
        task=entry.task,
        replicate=entry.replicate,
        started_at=started_at,
        db_event_count=len(db_events),
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
        builder_work_bin_executable_count=(
            result_evidence.builder_work_bin_executable_count
        ),
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


def summarize_cells(rows: list[RunRow]) -> dict[str, CellSummary]:
    summaries: dict[str, CellSummary] = {}
    for cell in sorted({row.cell for row in rows}):
        cell_rows = [row for row in rows if row.cell == cell]
        n = len(cell_rows)
        successes = sum(row.successful for row in cell_rows)
        terminal = sum(row.terminal for row in cell_rows)
        ci_low, ci_high = wilson_interval(successes, n)
        terminal_ci_low, terminal_ci_high = wilson_interval(successes, terminal)
        cost_observed_rows = [row for row in cell_rows if row.has_observed_cost]
        positive_cost_rows = [row for row in cost_observed_rows if row.total_cost_usd > 0.0]
        zero_cost_event_rows = [
            row for row in cost_observed_rows if row.total_cost_usd == 0.0
        ]
        costs = [row.total_cost_usd for row in cost_observed_rows]
        tokens = [float(row.total_tokens) for row in cost_observed_rows]
        durations = [
            row.run_duration_seconds
            for row in cell_rows
            if row.run_duration_seconds is not None
        ]
        tool_calls = [float(row.total_tool_calls) for row in cell_rows]
        summaries[cell] = CellSummary(
            cell=cell,
            n=n,
            terminal=terminal,
            nonterminal=n - terminal,
            successful=successes,
            success_rate=successes / n if n else 0.0,
            success_rate_ci_low=ci_low,
            success_rate_ci_high=ci_high,
            terminal_success_rate=successes / terminal if terminal else 0.0,
            terminal_success_rate_ci_low=terminal_ci_low,
            terminal_success_rate_ci_high=terminal_ci_high,
            auth_failures=sum(row.auth_failure for row in cell_rows),
            environmental_failures=sum(row.environmental_failure for row in cell_rows),
            auth_login_failures=sum(
                row.environmental_failure_cause == "auth_login" for row in cell_rows
            ),
            credit_quota_failures=sum(
                row.environmental_failure_cause == "credit_quota" for row in cell_rows
            ),
            provider_api_failures=sum(
                row.environmental_failure_cause == "provider_api" for row in cell_rows
            ),
            total_cost_usd=sum(costs),
            manager_cost_usd=sum(row.manager_cost_usd for row in cell_rows),
            worker_cost_usd=sum(row.worker_cost_usd for row in cell_rows),
            mean_cost_usd=_mean_or_none(costs),
            median_cost_usd=_median_or_none(costs),
            cost_per_success_usd=sum(costs) / successes if successes else None,
            cost_observed_runs=len(cost_observed_rows),
            positive_cost_runs=len(positive_cost_rows),
            zero_cost_event_runs=len(zero_cost_event_rows),
            missing_cost_runs=n - len(cost_observed_rows),
            total_tokens=sum(row.total_tokens for row in cost_observed_rows),
            mean_tokens=_mean_or_none(tokens),
            median_duration_seconds=_median_or_none(durations),
            total_tool_calls=sum(row.total_tool_calls for row in cell_rows),
            mean_tool_calls=_mean_or_none(tool_calls),
            task_mgmt_tool_calls=sum(row.task_mgmt_tool_calls for row in cell_rows),
            task_tool_calls=sum(row.task_tool_calls for row in cell_rows),
            forbidden_web_attempts=sum(row.forbidden_web_attempts for row in cell_rows),
            actual_tool_calls=sum(row.actual_tool_calls for row in cell_rows),
            cheat_tool_calls=sum(row.cheat_tool_calls for row in cell_rows),
            cheat_git_log_calls=sum(row.cheat_git_log_calls for row in cell_rows),
            cheat_git_show_calls=sum(row.cheat_git_show_calls for row in cell_rows),
            cheat_git_reflog_calls=sum(row.cheat_git_reflog_calls for row in cell_rows),
            cheat_git_diff_history_ref_calls=sum(
                row.cheat_git_diff_history_ref_calls for row in cell_rows
            ),
            cheat_git_diff_unparsed_calls=sum(
                row.cheat_git_diff_unparsed_calls for row in cell_rows
            ),
            recon_tool_calls=sum(row.recon_tool_calls for row in cell_rows),
            security_tool_calls=sum(row.security_tool_calls for row in cell_rows),
            subagent_tool_calls=sum(row.subagent_tool_calls for row in cell_rows),
            task_family_tool_calls=sum(row.task_family_tool_calls for row in cell_rows),
            task_create_tool_calls=sum(row.task_create_tool_calls for row in cell_rows),
            task_update_tool_calls=sum(row.task_update_tool_calls for row in cell_rows),
            task_list_tool_calls=sum(row.task_list_tool_calls for row in cell_rows),
            file_read_tool_calls=sum(row.file_read_tool_calls for row in cell_rows),
            file_write_tool_calls=sum(row.file_write_tool_calls for row in cell_rows),
            file_edit_tool_calls=sum(row.file_edit_tool_calls for row in cell_rows),
            search_tool_calls=sum(row.search_tool_calls for row in cell_rows),
            shell_tool_calls=sum(row.shell_tool_calls for row in cell_rows),
            web_forbidden_tool_calls=sum(row.web_forbidden_tool_calls for row in cell_rows),
            mcp_tool_calls=sum(row.mcp_tool_calls for row in cell_rows),
            other_tool_calls=sum(row.other_tool_calls for row in cell_rows),
            bash_recon_tool_calls=sum(row.bash_recon_tool_calls for row in cell_rows),
            bash_exploit_tool_calls=sum(row.bash_exploit_tool_calls for row in cell_rows),
            bash_security_scan_tool_calls=sum(
                row.bash_security_scan_tool_calls for row in cell_rows
            ),
            bash_build_tool_calls=sum(row.bash_build_tool_calls for row in cell_rows),
            bash_test_exec_tool_calls=sum(
                row.bash_test_exec_tool_calls for row in cell_rows
            ),
            bash_git_tool_calls=sum(row.bash_git_tool_calls for row in cell_rows),
            bash_other_shell_tool_calls=sum(
                row.bash_other_shell_tool_calls for row in cell_rows
            ),
            builder_real_successes=sum(row.builder_real_success for row in cell_rows),
            builder_evidence_present=sum(
                row.builder_evidence_present for row in cell_rows
            ),
            builder_executable_runs=sum(
                row.builder_executable_count > 0 for row in cell_rows
            ),
            builder_executable_count=sum(
                row.builder_executable_count for row in cell_rows
            ),
            exploiter_real_successes=sum(
                row.exploiter_real_success for row in cell_rows
            ),
            exploit_validation_present=sum(
                row.exploit_validation_present for row in cell_rows
            ),
            exploit_verdict_pass=sum(row.exploit_verdict_pass for row in cell_rows),
            exploit_error_match=sum(row.exploit_error_match for row in cell_rows),
            exploit_crash_function_match=sum(
                row.exploit_crash_function_match for row in cell_rows
            ),
            fixer_real_successes=sum(row.fixer_real_success for row in cell_rows),
            patch_validation_present=sum(
                row.patch_validation_present for row in cell_rows
            ),
            patch_verdict_pass=sum(row.patch_verdict_pass for row in cell_rows),
            patch_apply_clean=sum(row.patch_apply_clean for row in cell_rows),
            patch_build_success=sum(row.patch_build_success for row in cell_rows),
            patch_post_error_none=sum(row.patch_post_error_none for row in cell_rows),
            real_pipeline_successes=sum(
                row.real_pipeline_success for row in cell_rows
            ),
            model_patch_present=sum(row.model_patch_present for row in cell_rows),
            repro_script_present=sum(row.repro_script_present for row in cell_rows),
            security_report_present=sum(
                row.security_report_present for row in cell_rows
            ),
        )
    return summaries


def build_pair_rows(rows: list[RunRow]) -> list[PairRow]:
    a1 = _latest_pairable_rows(rows, cell="A1")
    a2 = _latest_pairable_rows(rows, cell="A2")
    pair_rows = []
    for task, replicate in sorted(set(a1) & set(a2)):
        left = a1[(task, replicate)]
        right = a2[(task, replicate)]
        included = left.terminal and right.terminal
        duration_delta = None
        if left.run_duration_seconds is not None and right.run_duration_seconds is not None:
            duration_delta = left.run_duration_seconds - right.run_duration_seconds
        cost_delta = None
        token_delta = None
        if left.has_observed_cost and right.has_observed_cost:
            cost_delta = left.total_cost_usd - right.total_cost_usd
            token_delta = left.total_tokens - right.total_tokens
        pair_rows.append(
            PairRow(
                task=task,
                replicate=replicate,
                a1_run_id=left.run_id,
                a2_run_id=right.run_id,
                pair_included=int(included),
                a1_successful=left.successful,
                a2_successful=right.successful,
                a1_environmental_failure=left.environmental_failure,
                a2_environmental_failure=right.environmental_failure,
                success_delta_a1_minus_a2=left.successful - right.successful,
                total_cost_delta_a1_minus_a2=cost_delta,
                duration_delta_a1_minus_a2=duration_delta,
                total_tokens_delta_a1_minus_a2=token_delta,
                tool_call_delta_a1_minus_a2=left.total_tool_calls - right.total_tool_calls,
                task_mgmt_tool_delta_a1_minus_a2=(
                    left.task_mgmt_tool_calls - right.task_mgmt_tool_calls
                ),
                task_tool_delta_a1_minus_a2=left.task_tool_calls - right.task_tool_calls,
                a1_failure_mode=left.failure_mode,
                a2_failure_mode=right.failure_mode,
                a1_total_cost_usd=left.total_cost_usd if left.has_observed_cost else None,
                a2_total_cost_usd=right.total_cost_usd if right.has_observed_cost else None,
                a1_duration_seconds=left.run_duration_seconds,
                a2_duration_seconds=right.run_duration_seconds,
                a1_total_tokens=left.total_tokens if left.has_observed_cost else None,
                a2_total_tokens=right.total_tokens if right.has_observed_cost else None,
                a1_total_tool_calls=left.total_tool_calls,
                a2_total_tool_calls=right.total_tool_calls,
                a1_actual_tool_calls=left.actual_tool_calls,
                a2_actual_tool_calls=right.actual_tool_calls,
                actual_tool_delta_a1_minus_a2=(
                    left.actual_tool_calls - right.actual_tool_calls
                ),
                a1_task_mgmt_tool_calls=left.task_mgmt_tool_calls,
                a2_task_mgmt_tool_calls=right.task_mgmt_tool_calls,
                a1_task_tool_calls=left.task_tool_calls,
                a2_task_tool_calls=right.task_tool_calls,
                task_family_tool_delta_a1_minus_a2=(
                    left.task_family_tool_calls - right.task_family_tool_calls
                ),
                a1_task_family_tool_calls=left.task_family_tool_calls,
                a2_task_family_tool_calls=right.task_family_tool_calls,
                task_create_tool_delta_a1_minus_a2=(
                    left.task_create_tool_calls - right.task_create_tool_calls
                ),
                a1_task_create_tool_calls=left.task_create_tool_calls,
                a2_task_create_tool_calls=right.task_create_tool_calls,
                task_update_tool_delta_a1_minus_a2=(
                    left.task_update_tool_calls - right.task_update_tool_calls
                ),
                a1_task_update_tool_calls=left.task_update_tool_calls,
                a2_task_update_tool_calls=right.task_update_tool_calls,
                task_list_tool_delta_a1_minus_a2=(
                    left.task_list_tool_calls - right.task_list_tool_calls
                ),
                a1_task_list_tool_calls=left.task_list_tool_calls,
                a2_task_list_tool_calls=right.task_list_tool_calls,
                a1_builder_real_success=left.builder_real_success,
                a2_builder_real_success=right.builder_real_success,
                builder_success_delta_a1_minus_a2=(
                    left.builder_real_success - right.builder_real_success
                ),
                a1_exploiter_real_success=left.exploiter_real_success,
                a2_exploiter_real_success=right.exploiter_real_success,
                exploiter_success_delta_a1_minus_a2=(
                    left.exploiter_real_success - right.exploiter_real_success
                ),
                a1_fixer_real_success=left.fixer_real_success,
                a2_fixer_real_success=right.fixer_real_success,
                fixer_success_delta_a1_minus_a2=(
                    left.fixer_real_success - right.fixer_real_success
                ),
                a1_real_pipeline_success=left.real_pipeline_success,
                a2_real_pipeline_success=right.real_pipeline_success,
                real_pipeline_success_delta_a1_minus_a2=(
                    left.real_pipeline_success - right.real_pipeline_success
                ),
                cheat_tool_delta_a1_minus_a2=(
                    left.cheat_tool_calls - right.cheat_tool_calls
                ),
                a1_cheat_tool_calls=left.cheat_tool_calls,
                a2_cheat_tool_calls=right.cheat_tool_calls,
                recon_tool_delta_a1_minus_a2=(
                    left.recon_tool_calls - right.recon_tool_calls
                ),
                a1_recon_tool_calls=left.recon_tool_calls,
                a2_recon_tool_calls=right.recon_tool_calls,
                security_tool_delta_a1_minus_a2=(
                    left.security_tool_calls - right.security_tool_calls
                ),
                a1_security_tool_calls=left.security_tool_calls,
                a2_security_tool_calls=right.security_tool_calls,
            )
        )
    return pair_rows


def _row_started_at(row: RunRow) -> datetime:
    return _parse_cutoff(row.started_at)


def _latest_pairable_rows(rows: list[RunRow], *, cell: str) -> dict[tuple[str, int], RunRow]:
    indexed: dict[tuple[str, int], RunRow] = {}
    for row in rows:
        if row.cell != cell:
            continue
        key = (row.task, row.replicate)
        existing = indexed.get(key)
        if existing is None or (
            _row_started_at(row),
            row.run_id,
        ) > (
            _row_started_at(existing),
            existing.run_id,
        ):
            indexed[key] = row
    return indexed


def _pairing_duplicate_summary(rows: list[RunRow]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, int], list[RunRow]] = {}
    for row in rows:
        grouped.setdefault((row.cell, row.task, row.replicate), []).append(row)
    duplicate_groups = {
        key: sorted(
            group,
            key=lambda row: (_row_started_at(row), row.run_id),
        )
        for key, group in grouped.items()
        if len(group) > 1
    }
    examples = []
    for (cell, task, replicate), group in sorted(duplicate_groups.items())[:20]:
        selected = group[-1]
        examples.append(
            {
                "cell": cell,
                "task": task,
                "replicate": replicate,
                "run_count": len(group),
                "selected_run_id": selected.run_id,
                "selected_started_at": selected.started_at,
                "all_run_ids": [row.run_id for row in group],
            }
        )
    return {
        "duplicate_pair_keys": len(duplicate_groups),
        "duplicate_pair_rows": sum(len(group) for group in duplicate_groups.values()),
        "pairing_duplicate_policy": "latest_started_at_then_run_id",
        "duplicate_pair_examples": examples,
    }


def _continuous_stats(pair_rows: list[PairRow], field_name: str) -> dict[str, Any]:
    values = [
        float(getattr(row, field_name))
        for row in pair_rows
        if row.pair_included and getattr(row, field_name) is not None
    ]
    positives = sum(value > 0 for value in values)
    negatives = sum(value < 0 for value in values)
    ci_low, ci_high = bootstrap_mean_ci(values)
    return {
        "n": len(values),
        "mean_delta": _mean_or_none(values),
        "median_delta": _median_or_none(values),
        "bootstrap_mean_ci_low": ci_low,
        "bootstrap_mean_ci_high": ci_high,
        "sign_test_p": exact_two_sided_binomial_p(positives, positives + negatives),
        "positive_deltas": positives,
        "negative_deltas": negatives,
        "zero_deltas": sum(value == 0 for value in values),
    }


def _paired_metric_stats(
    pair_rows: list[PairRow],
    *,
    metric: str,
    a1_field: str,
    a2_field: str,
    delta_field: str,
    value_kind: str,
    p_value: float | None,
) -> dict[str, Any]:
    values = [
        (
            float(getattr(row, a1_field)),
            float(getattr(row, a2_field)),
            float(getattr(row, delta_field)),
        )
        for row in pair_rows
        if row.pair_included
        and getattr(row, a1_field) is not None
        and getattr(row, a2_field) is not None
        and getattr(row, delta_field) is not None
    ]
    a1_values = [a1 for a1, _, _ in values]
    a2_values = [a2 for _, a2, _ in values]
    deltas = [delta for _, _, delta in values]
    return {
        "metric": metric,
        "n": len(values),
        "mean_a1": _mean_or_none(a1_values),
        "mean_a2": _mean_or_none(a2_values),
        "mean_delta": _mean_or_none(deltas),
        "median_a1": _median_or_none(a1_values),
        "median_a2": _median_or_none(a2_values),
        "median_delta": _median_or_none(deltas),
        "sign_or_exact_p": p_value,
        "value_kind": value_kind,
    }


def _paired_success_stats(pair_rows: list[PairRow]) -> dict[str, Any]:
    a1_only = sum(row.a1_successful == 1 and row.a2_successful == 0 for row in pair_rows)
    a2_only = sum(row.a1_successful == 0 and row.a2_successful == 1 for row in pair_rows)
    both_success = sum(row.a1_successful == 1 and row.a2_successful == 1 for row in pair_rows)
    neither = sum(row.a1_successful == 0 and row.a2_successful == 0 for row in pair_rows)
    success_deltas = [float(row.success_delta_a1_minus_a2) for row in pair_rows]
    ci_low, ci_high = bootstrap_mean_ci(success_deltas)
    discordant = a1_only + a2_only
    return {
        "paired_n": len(pair_rows),
        "both_success": both_success,
        "a1_only_success": a1_only,
        "a2_only_success": a2_only,
        "neither_success": neither,
        "paired_success_delta": _mean_or_none(success_deltas),
        "paired_success_delta_ci_low": ci_low,
        "paired_success_delta_ci_high": ci_high,
        "mcnemar_exact_p": exact_two_sided_binomial_p(a1_only, discordant),
        "discordant_pairs": discordant,
    }


def _with_environmental_failures_as_unsuccessful(row: PairRow) -> PairRow:
    a1_successful = int(row.a1_successful and not row.a1_environmental_failure)
    a2_successful = int(row.a2_successful and not row.a2_environmental_failure)
    return replace(
        row,
        a1_successful=a1_successful,
        a2_successful=a2_successful,
        success_delta_a1_minus_a2=a1_successful - a2_successful,
    )


def _paired_binary_stats(
    pair_rows: list[PairRow],
    *,
    a1_field: str,
    a2_field: str,
    delta_field: str,
) -> dict[str, Any]:
    a1_only = sum(
        getattr(row, a1_field) == 1 and getattr(row, a2_field) == 0
        for row in pair_rows
    )
    a2_only = sum(
        getattr(row, a1_field) == 0 and getattr(row, a2_field) == 1
        for row in pair_rows
    )
    both = sum(
        getattr(row, a1_field) == 1 and getattr(row, a2_field) == 1
        for row in pair_rows
    )
    neither = sum(
        getattr(row, a1_field) == 0 and getattr(row, a2_field) == 0
        for row in pair_rows
    )
    deltas = [float(getattr(row, delta_field)) for row in pair_rows]
    ci_low, ci_high = bootstrap_mean_ci(deltas)
    discordant = a1_only + a2_only
    return {
        "paired_n": len(pair_rows),
        "both": both,
        "a1_only": a1_only,
        "a2_only": a2_only,
        "neither": neither,
        "delta": _mean_or_none(deltas),
        "delta_ci_low": ci_low,
        "delta_ci_high": ci_high,
        "exact_p": exact_two_sided_binomial_p(a1_only, discordant),
        "discordant_pairs": discordant,
    }


def build_comparisons(pair_rows: list[PairRow]) -> dict[str, Any]:
    included = [row for row in pair_rows if row.pair_included]
    non_environmental = [
        row
        for row in included
        if not row.a1_environmental_failure and not row.a2_environmental_failure
    ]
    both_success_pairs = [
        row for row in included if row.a1_successful == 1 and row.a2_successful == 1
    ]
    raw_success = _paired_success_stats(included)
    non_environmental_success = _paired_success_stats(non_environmental)
    environmental_failures_as_unsuccessful = _paired_success_stats(
        [_with_environmental_failures_as_unsuccessful(row) for row in included]
    )
    cost_stats = _continuous_stats(included, "total_cost_delta_a1_minus_a2")
    duration_stats = _continuous_stats(included, "duration_delta_a1_minus_a2")
    token_stats = _continuous_stats(included, "total_tokens_delta_a1_minus_a2")
    tool_call_stats = _continuous_stats(included, "tool_call_delta_a1_minus_a2")
    actual_tool_call_stats = _continuous_stats(
        included,
        "actual_tool_delta_a1_minus_a2",
    )
    task_mgmt_stats = _continuous_stats(included, "task_mgmt_tool_delta_a1_minus_a2")
    subagent_stats = _continuous_stats(included, "task_tool_delta_a1_minus_a2")
    task_family_stats = _continuous_stats(included, "task_family_tool_delta_a1_minus_a2")
    task_create_stats = _continuous_stats(included, "task_create_tool_delta_a1_minus_a2")
    task_update_stats = _continuous_stats(included, "task_update_tool_delta_a1_minus_a2")
    task_list_stats = _continuous_stats(included, "task_list_tool_delta_a1_minus_a2")
    cheat_stats = _continuous_stats(included, "cheat_tool_delta_a1_minus_a2")
    recon_stats = _continuous_stats(included, "recon_tool_delta_a1_minus_a2")
    security_stats = _continuous_stats(included, "security_tool_delta_a1_minus_a2")
    a1_only = sum(row.a1_successful == 1 and row.a2_successful == 0 for row in included)
    a2_only = sum(row.a1_successful == 0 and row.a2_successful == 1 for row in included)
    both_success = sum(row.a1_successful == 1 and row.a2_successful == 1 for row in included)
    neither = sum(row.a1_successful == 0 and row.a2_successful == 0 for row in included)
    return {
        "paired_n": len(included),
        "observed_pair_n": len(pair_rows),
        "non_environmental_pair_n": len(non_environmental),
        "both_success": both_success,
        "a1_only_success": a1_only,
        "a2_only_success": a2_only,
        "neither_success": neither,
        "paired_success_delta": raw_success["paired_success_delta"],
        "paired_success_delta_ci_low": raw_success["paired_success_delta_ci_low"],
        "paired_success_delta_ci_high": raw_success["paired_success_delta_ci_high"],
        "mcnemar_exact_p": raw_success["mcnemar_exact_p"],
        "raw_success": raw_success,
        "non_environmental_success": non_environmental_success,
        "environmental_failures_as_unsuccessful": (
            environmental_failures_as_unsuccessful
        ),
        "metric_table": [
            _paired_metric_stats(
                included,
                metric="Success rate",
                a1_field="a1_successful",
                a2_field="a2_successful",
                delta_field="success_delta_a1_minus_a2",
                value_kind="percent",
                p_value=raw_success["mcnemar_exact_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Total cost",
                a1_field="a1_total_cost_usd",
                a2_field="a2_total_cost_usd",
                delta_field="total_cost_delta_a1_minus_a2",
                value_kind="usd",
                p_value=cost_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Duration seconds",
                a1_field="a1_duration_seconds",
                a2_field="a2_duration_seconds",
                delta_field="duration_delta_a1_minus_a2",
                value_kind="seconds",
                p_value=duration_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Tokens",
                a1_field="a1_total_tokens",
                a2_field="a2_total_tokens",
                delta_field="total_tokens_delta_a1_minus_a2",
                value_kind="count",
                p_value=token_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Total tool calls",
                a1_field="a1_total_tool_calls",
                a2_field="a2_total_tool_calls",
                delta_field="tool_call_delta_a1_minus_a2",
                value_kind="count",
                p_value=tool_call_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Actual tool calls",
                a1_field="a1_actual_tool_calls",
                a2_field="a2_actual_tool_calls",
                delta_field="actual_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=actual_tool_call_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Task-family calls",
                a1_field="a1_task_family_tool_calls",
                a2_field="a2_task_family_tool_calls",
                delta_field="task_family_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=task_family_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Task-management calls",
                a1_field="a1_task_mgmt_tool_calls",
                a2_field="a2_task_mgmt_tool_calls",
                delta_field="task_mgmt_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=task_mgmt_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Subagent-spawn calls",
                a1_field="a1_task_tool_calls",
                a2_field="a2_task_tool_calls",
                delta_field="task_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=subagent_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Cheat calls",
                a1_field="a1_cheat_tool_calls",
                a2_field="a2_cheat_tool_calls",
                delta_field="cheat_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=cheat_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Recon calls",
                a1_field="a1_recon_tool_calls",
                a2_field="a2_recon_tool_calls",
                delta_field="recon_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=recon_stats["sign_test_p"],
            ),
            _paired_metric_stats(
                included,
                metric="Security-tool calls",
                a1_field="a1_security_tool_calls",
                a2_field="a2_security_tool_calls",
                delta_field="security_tool_delta_a1_minus_a2",
                value_kind="count",
                p_value=security_stats["sign_test_p"],
            ),
        ],
        "cost": cost_stats,
        "duration": duration_stats,
        "tokens": token_stats,
        "tool_calls": tool_call_stats,
        "actual_tool_calls": actual_tool_call_stats,
        "non_environmental_resources": {
            "cost": _continuous_stats(non_environmental, "total_cost_delta_a1_minus_a2"),
            "duration": _continuous_stats(non_environmental, "duration_delta_a1_minus_a2"),
            "tokens": _continuous_stats(non_environmental, "total_tokens_delta_a1_minus_a2"),
        },
        "both_success_resources": {
            "cost": _continuous_stats(both_success_pairs, "total_cost_delta_a1_minus_a2"),
            "duration": _continuous_stats(both_success_pairs, "duration_delta_a1_minus_a2"),
            "tokens": _continuous_stats(both_success_pairs, "total_tokens_delta_a1_minus_a2"),
        },
        "task_mgmt_tool_calls": task_mgmt_stats,
        "subagent_tool_calls": subagent_stats,
        "task_tool_calls": subagent_stats,
        "task_family_tool_calls": task_family_stats,
        "task_create_tool_calls": task_create_stats,
        "task_update_tool_calls": task_update_stats,
        "task_list_tool_calls": task_list_stats,
        "cheat_tool_calls": cheat_stats,
        "recon_tool_calls": recon_stats,
        "security_tool_calls": security_stats,
        "builder_real_success": _paired_binary_stats(
            included,
            a1_field="a1_builder_real_success",
            a2_field="a2_builder_real_success",
            delta_field="builder_success_delta_a1_minus_a2",
        ),
        "exploiter_real_success": _paired_binary_stats(
            included,
            a1_field="a1_exploiter_real_success",
            a2_field="a2_exploiter_real_success",
            delta_field="exploiter_success_delta_a1_minus_a2",
        ),
        "fixer_real_success": _paired_binary_stats(
            included,
            a1_field="a1_fixer_real_success",
            a2_field="a2_fixer_real_success",
            delta_field="fixer_success_delta_a1_minus_a2",
        ),
        "real_pipeline_success": _paired_binary_stats(
            included,
            a1_field="a1_real_pipeline_success",
            a2_field="a2_real_pipeline_success",
            delta_field="real_pipeline_success_delta_a1_minus_a2",
        ),
    }


def _failure_mode_counts(rows: list[RunRow]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for cell in sorted({row.cell for row in rows}):
        counts = Counter(
            _failure_mode_label(row)
            for row in rows
            if row.cell == cell and row.successful == 0
        )
        result[cell] = dict(sorted(counts.items()))
    return result


def _failure_mode_label(row: RunRow) -> str:
    return row.failure_mode or NO_CLASSIFIED_FAILURE


def _cell_value(cells: dict[str, CellSummary], cell: str, field_name: str) -> Any:
    return getattr(cells[cell], field_name)


def _tool_use_rows(
    cells: dict[str, CellSummary],
    *,
    cell_names: tuple[str, ...] = ("A1", "A2"),
) -> list[dict[str, Any]]:
    fields = [
        ("total_tool_calls_avg", "total_tool_calls"),
        ("actual_tool_calls_avg", "actual_tool_calls"),
        ("cheat_calls_avg", "cheat_tool_calls"),
        ("recon_calls_avg", "recon_tool_calls"),
        ("security_tool_calls_avg", "security_tool_calls"),
        ("subagent_spawns_avg", "subagent_tool_calls"),
        ("task_family_calls_avg", "task_family_tool_calls"),
        ("task_mgmt_calls_avg", "task_mgmt_tool_calls"),
        ("task_create_avg", "task_create_tool_calls"),
        ("task_update_avg", "task_update_tool_calls"),
        ("task_list_avg", "task_list_tool_calls"),
        ("shell_calls_avg", "shell_tool_calls"),
        ("file_read_avg", "file_read_tool_calls"),
        ("file_write_avg", "file_write_tool_calls"),
        ("file_edit_avg", "file_edit_tool_calls"),
        ("search_avg", "search_tool_calls"),
    ]
    rows: list[dict[str, Any]] = []
    for cell in cell_names:
        summary = cells[cell]
        row: dict[str, Any] = {"cell": cell, "n": summary.n}
        for output_key, field_name in fields:
            total = float(getattr(summary, field_name))
            row[output_key] = total / summary.n if summary.n else None
        rows.append(row)
    return rows


def _cheat_analysis(
    rows: list[RunRow],
    *,
    cells: tuple[str, ...] = ("A1", "A2"),
) -> list[dict[str, Any]]:
    analysis_rows = []
    for key, label, definition, field_name in CHEAT_PATTERN_FIELDS:
        row: dict[str, Any] = {
            "pattern": key,
            "label": label,
            "definition": definition,
        }
        for cell in cells:
            cell_rows = [run_row for run_row in rows if run_row.cell == cell]
            calls = sum(int(getattr(run_row, field_name)) for run_row in cell_rows)
            row[f"{cell}_calls"] = calls
            row[f"{cell}_avg_per_run"] = calls / len(cell_rows) if cell_rows else None
            row[f"{cell}_runs"] = sum(
                int(getattr(run_row, field_name)) > 0 for run_row in cell_rows
            )
        analysis_rows.append(row)
    return analysis_rows


def _duplicate_accounting(entries: list[EnrollmentEntry]) -> list[dict[str, Any]]:
    accounting = []
    for cell in ("A1", "A2"):
        cell_entries = [entry for entry in entries if entry.cell == cell]
        key_counts = Counter((entry.task, entry.replicate) for entry in cell_entries)
        duplicate_counts = [count for count in key_counts.values() if count > 1]
        accounting.append(
            {
                "cell": cell,
                "enrolled_rows": len(cell_entries),
                "unique_task_replicates": len(key_counts),
                "duplicate_keys": len(duplicate_counts),
                "duplicate_rows": sum(duplicate_counts),
                "duplicate_extra_rows": sum(count - 1 for count in duplicate_counts),
            }
        )
    return accounting


def _start_date_accounting(entries: list[EnrollmentEntry]) -> list[dict[str, Any]]:
    dates = sorted({entry.started_at[:10] for entry in entries})
    rows_by_date = []
    for date in dates:
        rows_by_date.append(
            {
                "date": date,
                "A1": sum(
                    entry.cell == "A1" and entry.started_at.startswith(date) for entry in entries
                ),
                "A2": sum(
                    entry.cell == "A2" and entry.started_at.startswith(date) for entry in entries
                ),
            }
        )
    return rows_by_date


def _status_order(status: str) -> int:
    order = {
        "completed": 0,
        "in_progress": 1,
        "timed_out": 2,
        "failed": 3,
    }
    return order.get(status, 4)


def _failure_phase(row: RunRow) -> str:
    if row.environmental_failure_cause == "provider_api":
        return "provider/api"
    if row.builder_real_success == 0:
        return "builder"
    if row.exploiter_real_success == 0:
        return "exploiter"
    if row.fixer_real_success == 0:
        return "fixer"
    if row.security_report_present == 0:
        return "reporter"
    return "post-evidence orchestration"


def _count_table(
    labels: list[str],
    rows: list[RunRow],
    *,
    key_name: str,
    key_func: Any,
    cells: tuple[str, ...] = ("A1", "A2"),
) -> list[dict[str, Any]]:
    table = []
    for label in labels:
        entry: dict[str, Any] = {key_name: label}
        for cell in cells:
            entry[cell] = sum(
                row.cell == cell and key_func(row) == label for row in rows
            )
        table.append(entry)
    return table


def _failure_analysis(
    rows: list[RunRow],
    *,
    cells: tuple[str, ...] = ("A1", "A2"),
) -> dict[str, Any]:
    non_success = [row for row in rows if row.successful == 0]
    classified_failures = [row for row in non_success if row.failure_mode]
    non_login_credit = [
        row
        for row in classified_failures
        if row.environmental_failure_cause not in {"auth_login", "credit_quota"}
    ]
    status_labels = sorted({row.run_status for row in rows}, key=_status_order)
    mode_labels = sorted({_failure_mode_label(row) for row in classified_failures})
    phase_labels = [
        "builder",
        "exploiter",
        "fixer",
        "reporter",
        "provider/api",
        "post-evidence orchestration",
    ]
    return {
        "status_counts": _count_table(
            status_labels,
            rows,
            key_name="status",
            key_func=lambda row: row.run_status,
            cells=cells,
        ),
        "failure_mode_counts": _count_table(
            mode_labels,
            classified_failures,
            key_name="classification",
            key_func=_failure_mode_label,
            cells=cells,
        ),
        "phase_counts_excluding_login_credit": _count_table(
            phase_labels,
            non_login_credit,
            key_name="phase",
            key_func=_failure_phase,
            cells=cells,
        ),
        "excluded_login_auth": {
            cell: sum(
                row.cell == cell and row.environmental_failure_cause == "auth_login"
                for row in non_success
            )
            for cell in cells
        },
        "excluded_credit_quota": {
            cell: sum(
                row.cell == cell and row.environmental_failure_cause == "credit_quota"
                for row in non_success
            )
            for cell in cells
        },
    }


def _unmatched_tasks(rows: list[RunRow]) -> dict[str, list[str]]:
    a1_tasks = {(row.task, row.replicate) for row in rows if row.cell == "A1"}
    a2_tasks = {(row.task, row.replicate) for row in rows if row.cell == "A2"}
    return {
        "a1_without_a2": [f"{task}#{rep}" for task, rep in sorted(a1_tasks - a2_tasks)],
        "a2_without_a1": [f"{task}#{rep}" for task, rep in sorted(a2_tasks - a1_tasks)],
    }


def _per_run_breakdown(rows: list[RunRow]) -> list[dict[str, Any]]:
    """Return compact row-level metrics for the Markdown report."""
    breakdown: list[dict[str, Any]] = []
    for row in sorted(
        rows,
        key=lambda r: (
            _status_order(r.run_status),
            r.failure_mode,
            r.task,
            r.replicate,
            r.cell,
            r.run_id,
        ),
    ):
        breakdown.append(
            {
                "task": row.task,
                "replicate": row.replicate,
                "cell": row.cell,
                "run_id": row.run_id[:8],
                "started_at": row.started_at,
                "status": row.run_status,
                "status_detail": _status_detail(row),
                "failure_mode": row.failure_mode,
                "successful": row.successful,
                "total_cost_usd": row.total_cost_usd if row.has_observed_cost else None,
                "actual_tool_calls": row.actual_tool_calls,
                "cheat_tool_calls": row.cheat_tool_calls,
                "recon_tool_calls": row.recon_tool_calls,
                "security_tool_calls": row.security_tool_calls,
                "subagent_tool_calls": row.subagent_tool_calls,
                "task_family_tool_calls": row.task_family_tool_calls,
                "task_create_tool_calls": row.task_create_tool_calls,
                "task_update_tool_calls": row.task_update_tool_calls,
                "task_list_tool_calls": row.task_list_tool_calls,
                "builder_real_success": row.builder_real_success,
                "exploiter_real_success": row.exploiter_real_success,
                "fixer_real_success": row.fixer_real_success,
                "real_pipeline_success": row.real_pipeline_success,
            }
        )
    return breakdown


def _status_detail(row: RunRow) -> str:
    if row.failure_mode:
        return f"{row.run_status} / {row.failure_mode}"
    if row.successful == 0:
        return f"{row.run_status} / {NO_CLASSIFIED_FAILURE}"
    return row.run_status


def _ignored_run_dirs(entries: list[EnrollmentEntry]) -> list[str]:
    enrolled = {entry.run_id for entry in entries}
    extra: set[str] = set()
    for manifest_path in iter_run_manifests():
        manifest = read_run_manifest(manifest_path)
        if (
            manifest.get("study_id") != STUDY_ID
            or manifest.get("cell") not in {"A1", "A2"}
        ):
            continue
        run_id = manifest.get("run_id")
        if isinstance(run_id, str) and run_id not in enrolled:
            extra.add(run_id)
    return sorted(extra)


def _round_nested(value: Any) -> Any:
    if type(value) is float:
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round_nested(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_nested(item) for item in value]
    return value


def build_summary(
    *,
    rows: list[RunRow],
    pair_rows: list[PairRow],
    entries: list[EnrollmentEntry],
    pre_dedup_entries: list[EnrollmentEntry] | None = None,
    discovery: StudyRunDiscovery,
    lock_entry_count: int,
    lock_load_error: str,
    lock_discovered_run_id_disagreements: list[str],
    started_before: datetime,
    files_by_run_id: dict[str, RunFiles],
) -> dict[str, Any]:
    if pre_dedup_entries is None:
        pre_dedup_entries = entries
    cell_summaries = summarize_cells(rows)
    comparisons = build_comparisons(pair_rows)
    pairing_duplicates = _pairing_duplicate_summary(rows)
    summary = {
        "study_id": STUDY_ID,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "design": {
            "A1": "flat Claude Code CLI with Task subagents enabled",
            "A2": "flat Claude Code CLI with Task subagents disabled",
            "worker_model": "claude-sonnet-4-5-20250929",
            "judge": "disabled by orchestration.skip_judge=true",
        },
        "data_integrity": {
            "source_scope": "db_events_intersect_top_level_runs",
            "dataset_task_count": discovery.dataset_task_count,
            "db_candidate_runs": len(discovery.db_candidate_run_ids),
            "top_level_run_event_files": (
                len(discovery.entries) + len(discovery.local_runfiles_missing_db_candidates)
            ),
            "db_candidates_missing_top_level_runfiles": len(
                discovery.db_candidates_missing_runfiles
            ),
            "local_runfiles_missing_db_candidates": len(
                discovery.local_runfiles_missing_db_candidates
            ),
            "lock_enrolled_runs": lock_entry_count,
            "lock_load_error": lock_load_error,
            "lock_discovered_run_id_disagreements": lock_discovered_run_id_disagreements,
            "source_runfile_entries": len(discovery.entries),
            "enrolled_runs": len(entries),
            "pre_dedup_enrolled_runs": len(pre_dedup_entries),
            "excluded_by_started_before_cutoff": len(discovery.entries) - len(pre_dedup_entries),
            "excluded_by_cve_dedup": len(pre_dedup_entries) - len(entries),
            "enrollment_dedup_policy": "latest_started_at_then_run_id_per_cell_task",
            "started_before_cutoff": started_before.isoformat().replace("+00:00", "Z"),
            "started_before_cutoff_local": _format_cutoff_local(started_before),
            "computed_rows": len(rows),
            "a1_enrolled": sum(entry.cell == "A1" for entry in entries),
            "a2_enrolled": sum(entry.cell == "A2" for entry in entries),
            "terminal_rows": sum(row.terminal for row in rows),
            "nonterminal_rows": sum(row.terminal == 0 for row in rows),
            "missing_cost_rows": sum(row.has_observed_cost == 0 for row in rows),
            "environmental_failures": sum(row.environmental_failure for row in rows),
            "db_runs_event_id_mismatches": sum(
                row.db_runs_event_ids_match == 0 for row in rows
            ),
            "db_runs_event_record_mismatches": sum(
                row.db_runs_event_records_match == 0 for row in rows
            ),
            "db_runs_cost_mismatches": sum(row.db_runs_cost_match == 0 for row in rows),
            "pairing_duplicate_keys": pairing_duplicates["duplicate_pair_keys"],
            "pairing_duplicate_rows": pairing_duplicates["duplicate_pair_rows"],
            "pairing_duplicate_policy": pairing_duplicates["pairing_duplicate_policy"],
            "pairing_duplicate_examples": pairing_duplicates["duplicate_pair_examples"],
            "ignored_run_dirs": discovery.local_runfiles_missing_db_candidates,
            "lock_runfile_pre_cutoff_disagreements": lock_discovered_run_id_disagreements,
        },
        "cells": {cell: asdict(summary) for cell, summary in cell_summaries.items()},
        "prompt_samples": _prompt_samples(entries, files_by_run_id),
        "tool_use_rows": _tool_use_rows(cell_summaries),
        "cheat_analysis": _cheat_analysis(rows),
        "enrollment_accounting": {
            "by_cell": _duplicate_accounting(pre_dedup_entries),
            "by_started_date": _start_date_accounting(pre_dedup_entries),
        },
        "paired": comparisons,
        "failure_modes": _failure_mode_counts(rows),
        "failure_analysis": _failure_analysis(rows),
        "unmatched_tasks": _unmatched_tasks(rows),
        "per_run_breakdown": _per_run_breakdown(rows),
    }
    return _round_nested(summary)


async def build_rows(
    entries: list[EnrollmentEntry],
    files_by_run_id: dict[str, RunFiles],
) -> list[RunRow]:
    conn = await open_connection()
    try:
        rows = []
        for index, entry in enumerate(entries, start=1):
            rows.append(await build_run_row(conn, entry, files_by_run_id[entry.run_id]))
            if index % 25 == 0:
                logger.info("processed %s/%s enrolled runs", index, len(entries))
        return rows
    finally:
        await conn.close()


def _csv_bytes(rows: list[Any]) -> bytes:
    if not rows:
        return b""
    buffer = io.StringIO()
    fieldnames = list(asdict(rows[0]).keys())
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(asdict(row))
    return buffer.getvalue().encode("utf-8")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{100 * value:.1f}%"


def _format_number(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def _format_usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.2f}"


def _format_p(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value < 0.0001:
        return "<0.0001"
    return f"{value:.4f}"


def _format_metric_value(value: float | int | None, value_kind: str) -> str:
    if value_kind == "percent":
        return _format_percent(None if value is None else float(value))
    if value_kind == "usd":
        return _format_usd(None if value is None else float(value))
    if value_kind == "seconds":
        return _format_number(None if value is None else float(value), 1)
    if value_kind == "count":
        return _format_number(None if value is None else float(value), 1)
    return _format_number(None if value is None else float(value), 2)


def render_report(summary: dict[str, Any]) -> str:
    template_abs = _repo_path(TEMPLATE_PATH)
    env = Environment(
        loader=FileSystemLoader(str(template_abs.parent)),
        autoescape=False,  # noqa: S701 - markdown report, not HTML rendering.
        undefined=StrictUndefined,
    )
    env.filters["percent"] = _format_percent
    env.filters["num"] = _format_number
    env.filters["usd"] = _format_usd
    env.filters["p"] = _format_p
    env.filters["metric"] = _format_metric_value
    template = env.get_template(template_abs.name)
    return template.render(summary=summary)


def write_outputs(
    *,
    rows: list[RunRow],
    pair_rows: list[PairRow],
    summary: dict[str, Any],
    entries: list[EnrollmentEntry],
    files_by_run_id: dict[str, RunFiles],
) -> dict[str, str]:
    reports_dir = _repo_path("experiments", STUDY_ID, "reports")
    inputs = base_input_paths() + run_input_paths(entries, files_by_run_id)
    metrics_path = reports_dir / f"{REPORT_BASENAME}_metrics.csv"
    pairs_path = reports_dir / f"{REPORT_BASENAME}_pairs.csv"
    summary_path = reports_dir / f"{REPORT_BASENAME}_summary.json"
    report_path = reports_dir / f"{REPORT_BASENAME}_report.md"

    write_binary(path=metrics_path, content=_csv_bytes(rows), script=__file__, inputs=inputs)
    write_binary(path=pairs_path, content=_csv_bytes(pair_rows), script=__file__, inputs=inputs)
    write_binary(path=summary_path, content=_json_bytes(summary), script=__file__, inputs=inputs)
    report_inputs = [metrics_path, pairs_path, summary_path]
    report_body = render_report(summary)
    write_md(
        path=report_path,
        content=report_body,
        script=__file__,
        template=TEMPLATE_PATH,
        inputs=report_inputs,
    )
    return {
        "metrics": to_repo_relative(metrics_path),
        "pairs": to_repo_relative(pairs_path),
        "summary": to_repo_relative(summary_path),
        "report": to_repo_relative(report_path),
    }


async def run_analysis(started_before: datetime) -> dict[str, Any]:
    lock_entries, lock_load_error = _load_lock_entries_for_audit(STUDY_ID)
    conn = await open_connection()
    try:
        discovery = await discover_study_runs(conn, STUDY_ID)
    finally:
        await conn.close()
    pre_dedup_entries = filter_analysis_cohort(
        discovery.entries,
        discovery.files_by_run_id,
        started_before=started_before,
    )
    entries = _dedupe_latest_per_cell_task(pre_dedup_entries)
    discovered_run_ids = {entry.run_id for entry in discovery.entries}
    lock_run_ids = {entry.run_id for entry in lock_entries}
    lock_disagreements = sorted(discovered_run_ids ^ lock_run_ids)
    files_by_run_id = {entry.run_id: discovery.files_by_run_id[entry.run_id] for entry in entries}
    rows = await build_rows(entries, files_by_run_id)
    pair_rows = build_pair_rows(rows)
    summary = build_summary(
        rows=rows,
        pair_rows=pair_rows,
        entries=entries,
        pre_dedup_entries=pre_dedup_entries,
        discovery=discovery,
        lock_entry_count=len(lock_entries),
        lock_load_error=lock_load_error,
        lock_discovered_run_id_disagreements=lock_disagreements,
        started_before=started_before,
        files_by_run_id=files_by_run_id,
    )
    outputs = write_outputs(
        rows=rows,
        pair_rows=pair_rows,
        summary=summary,
        entries=entries,
        files_by_run_id=files_by_run_id,
    )
    return {"summary": summary, "outputs": outputs}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--started-before",
        default=DEFAULT_STARTED_BEFORE,
        help="Only analyze discovered runs whose DB RunStarted time is before this ISO timestamp.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper()))
    result = asyncio.run(run_analysis(_parse_cutoff(str(args.started_before))))
    logger.info("wrote A1/A2 analysis outputs: %s", result["outputs"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
