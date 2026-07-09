"""Tests for the A1/A2 analysis report helpers."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from experiments.shared.scripts.a12_analysis import (
    EnrollmentEntry,
    RunFiles,
    RunRow,
    StudyRunCandidate,
    _build_discovery_from_candidates,
    _cheat_analysis,
    _costs_match,
    _crash_function_matches,
    _failure_analysis,
    _per_run_breakdown,
    _prompt_samples,
    _real_pipeline_success,
    _sanitizer_error_matches,
    base_input_paths,
    build_comparisons,
    build_pair_rows,
    compute_result_evidence,
    compute_tool_audit,
    event_records_match,
    exact_two_sided_binomial_p,
    filter_analysis_cohort,
    load_runfile_entries,
    resolve_run_files,
    summarize_cells,
)
from experiments.shared.scripts.analysis.metrics.tools import compute_tools
from experiments.shared.scripts.analysis.tests.factories import (
    event_row,
    tool_use_event,
    worker_cost_recorded,
)
from experiments.shared.scripts.analysis.text.cheating_detector import (
    _cheating_pattern,
    _is_cheating_command,
)
from experiments.shared.scripts.db.models import EventRow


if TYPE_CHECKING:
    from pathlib import Path


def _row(
    *,
    cell: str,
    task: str,
    successful: int,
    total_cost_usd: float,
    task_tool_calls: int = 0,
    run_id: str | None = None,
    started_at: str = "2026-05-17T12:00:00Z",
) -> RunRow:
    return RunRow(
        source_authority="test",
        run_id=run_id or f"{cell}-{task}",
        cell=cell,
        task=task,
        replicate=0,
        started_at=started_at,
        db_event_count=1,
        runs_event_count=1,
        db_runs_event_ids_match=1,
        db_runs_event_records_match=1,
        db_runs_cost_match=1,
        terminal=1,
        successful=successful,
        run_status="completed" if successful else "failed",
        failure_mode="" if successful else "work_error",
        failure_reason="",
        auth_failure=0,
        environmental_failure=0,
        environmental_failure_cause="",
        manager_cost_usd=0.0,
        worker_cost_usd=total_cost_usd,
        total_cost_usd=total_cost_usd,
        has_observed_cost=1,
        total_tokens=100,
        prompt_tokens=10,
        completion_tokens=20,
        cache_read_tokens=60,
        cache_write_tokens=10,
        reasoning_tokens=0,
        run_duration_seconds=30.0,
        wall_clock_seconds=30.0,
        total_tool_calls=5,
        bash_tool_calls=1,
        task_mgmt_tool_calls=task_tool_calls,
        task_tool_calls=task_tool_calls,
        forbidden_web_attempts=0,
        worker_cost_event_count=1,
        aggregate_count=1,
    )


def _event(payload: dict, metadata: dict | None = None) -> EventRow:
    return EventRow(
        event_id=uuid4(),
        aggregate_id=uuid4(),
        sequence_number=1,
        event_type="ThoughtCaptured",
        payload=payload,
        occurred_at=datetime(2026, 5, 17, 12, tzinfo=UTC),
        metadata=metadata or {},
    )


def test_build_pair_rows_matches_on_task_and_replicate() -> None:
    # Given: A1 and A2 rows with one matched task and one unmatched A2 task.
    rows = [
        _row(cell="A1", task="cve-a", successful=1, total_cost_usd=1.25, task_tool_calls=3),
        _row(cell="A2", task="cve-a", successful=0, total_cost_usd=0.75),
        _row(cell="A2", task="cve-b", successful=1, total_cost_usd=0.50),
    ]

    # When: paired rows are built.
    pairs = build_pair_rows(rows)

    # Then: only the matched task is paired and deltas are A1 minus A2.
    assert len(pairs) == 1
    assert pairs[0].task == "cve-a"
    assert pairs[0].success_delta_a1_minus_a2 == 1
    assert pairs[0].total_cost_delta_a1_minus_a2 == 0.5
    assert pairs[0].task_mgmt_tool_delta_a1_minus_a2 == 3
    assert pairs[0].task_tool_delta_a1_minus_a2 == 3


def test_build_pair_rows_marks_nonterminal_pairs_not_included() -> None:
    # Given: a matched pair where A1 has not reached a terminal RunCompleted.
    left = _row(cell="A1", task="cve-a", successful=0, total_cost_usd=0.0)
    left = replace(left, terminal=0, run_status="in_progress")
    right = _row(cell="A2", task="cve-a", successful=1, total_cost_usd=1.0)

    # When: paired rows are built.
    pairs = build_pair_rows([left, right])

    # Then: the pair remains visible but is excluded from terminal inference.
    assert len(pairs) == 1
    assert pairs[0].pair_included == 0


def test_build_pair_rows_uses_missing_cost_as_missing_delta() -> None:
    # Given: a terminal pair where A1 has no observed cost event.
    left = _row(cell="A1", task="cve-a", successful=0, total_cost_usd=0.0)
    left = replace(left, has_observed_cost=0)
    right = _row(cell="A2", task="cve-a", successful=0, total_cost_usd=1.0)

    # When: paired rows are built.
    pairs = build_pair_rows([left, right])

    # Then: cost and token deltas are missing, not fake zero/negative values.
    assert pairs[0].pair_included == 1
    assert pairs[0].total_cost_delta_a1_minus_a2 is None
    assert pairs[0].total_tokens_delta_a1_minus_a2 is None


def test_build_pair_rows_uses_latest_duplicate_for_pairing() -> None:
    # Given: two A1 rows with the same pairing key from repeated experiment attempts.
    rows = [
        _row(
            cell="A1",
            task="cve-a",
            successful=0,
            total_cost_usd=2.0,
            run_id="older-a1",
            started_at="2026-05-17T12:00:00Z",
        ),
        _row(
            cell="A1",
            task="cve-a",
            successful=1,
            total_cost_usd=1.0,
            run_id="newer-a1",
            started_at="2026-05-18T06:00:00Z",
        ),
        _row(cell="A2", task="cve-a", successful=1, total_cost_usd=1.0),
    ]

    # When: paired rows are built.
    pairs = build_pair_rows(rows)

    # Then: the latest-started A1 run is selected deterministically for inference.
    assert pairs[0].a1_run_id == "newer-a1"
    assert pairs[0].success_delta_a1_minus_a2 == 0


def test_build_comparisons_reports_environmental_sensitivity() -> None:
    # Given: one raw A2-only success caused by an A1 environmental failure
    # and one clean A1-only success.
    environmental_a1 = _row(
        cell="A1",
        task="cve-env",
        successful=0,
        total_cost_usd=0.0,
    )
    environmental_a1 = replace(
        environmental_a1,
        environmental_failure=1,
        environmental_failure_cause="credit_quota",
    )
    rows = [
        environmental_a1,
        _row(cell="A2", task="cve-env", successful=1, total_cost_usd=1.0),
        _row(cell="A1", task="cve-clean", successful=1, total_cost_usd=1.0),
        _row(cell="A2", task="cve-clean", successful=0, total_cost_usd=1.0),
    ]

    # When: comparisons are computed from the paired rows.
    comparisons = build_comparisons(build_pair_rows(rows))

    # Then: raw terminal data is discordant, while the clean sensitivity
    # keeps only the non-environmental pair.
    assert comparisons["raw_success"]["a1_only_success"] == 1
    assert comparisons["raw_success"]["a2_only_success"] == 1
    assert comparisons["non_environmental_success"]["paired_n"] == 1
    assert comparisons["non_environmental_success"]["a1_only_success"] == 1
    assert comparisons["non_environmental_success"]["a2_only_success"] == 0
    assert comparisons["environmental_failures_as_unsuccessful"]["paired_n"] == 2
    assert comparisons["environmental_failures_as_unsuccessful"]["a1_only_success"] == 1
    assert comparisons["environmental_failures_as_unsuccessful"]["a2_only_success"] == 1


def test_build_comparisons_can_impute_environmental_success_as_unsuccessful() -> None:
    # Given: a future-shaped pair where a run is marked successful but also
    # environmental. This guards the sensitivity calculation, not current data.
    environmental_a1 = replace(
        _row(cell="A1", task="cve-env", successful=1, total_cost_usd=1.0),
        environmental_failure=1,
        environmental_failure_cause="provider_api",
    )
    rows = [
        environmental_a1,
        _row(cell="A2", task="cve-env", successful=0, total_cost_usd=1.0),
    ]

    # When: comparisons are computed.
    comparisons = build_comparisons(build_pair_rows(rows))

    # Then: raw success preserves observed status, while imputed success treats
    # the environmental side as unsuccessful.
    assert comparisons["raw_success"]["a1_only_success"] == 1
    assert comparisons["environmental_failures_as_unsuccessful"]["a1_only_success"] == 0
    assert comparisons["environmental_failures_as_unsuccessful"]["neither_success"] == 1


def test_summarize_cells_keeps_manager_worker_total_costs_separate() -> None:
    # Given: Two A1 rows with worker-only cost and different outcomes.
    rows = [
        _row(cell="A1", task="cve-a", successful=1, total_cost_usd=1.0),
        _row(cell="A1", task="cve-b", successful=0, total_cost_usd=3.0),
    ]

    # When: cell summaries are computed.
    summary = summarize_cells(rows)["A1"]

    # Then: worker cost and total cost match while manager cost remains zero.
    assert summary.n == 2
    assert summary.terminal == 2
    assert summary.successful == 1
    assert summary.manager_cost_usd == 0.0
    assert summary.worker_cost_usd == 4.0
    assert summary.total_cost_usd == 4.0
    assert summary.cost_per_success_usd == 4.0
    assert summary.cost_observed_runs == 2
    assert summary.positive_cost_runs == 2
    assert summary.zero_cost_event_runs == 0
    assert summary.missing_cost_runs == 0


def test_summarize_cells_excludes_missing_cost_from_cost_means() -> None:
    # Given: One observed-cost row and one row with no cost event.
    observed = _row(cell="A1", task="cve-a", successful=1, total_cost_usd=2.0)
    missing = _row(cell="A1", task="cve-b", successful=0, total_cost_usd=0.0)
    missing = replace(missing, has_observed_cost=0)

    # When: cell summaries are computed.
    summary = summarize_cells([observed, missing])["A1"]

    # Then: missing-cost rows are counted but do not lower mean cost.
    assert summary.n == 2
    assert summary.cost_observed_runs == 1
    assert summary.positive_cost_runs == 1
    assert summary.zero_cost_event_runs == 0
    assert summary.missing_cost_runs == 1
    assert summary.mean_cost_usd == 2.0


def test_per_run_breakdown_keeps_core_metrics_and_cost_missingness() -> None:
    # Given: unsorted run rows with one observed-cost row and one missing-cost row.
    observed = replace(
        _row(cell="A2", task="zlib.cve-1", successful=1, total_cost_usd=2.0),
        actual_tool_calls=10,
        cheat_tool_calls=1,
        recon_tool_calls=4,
        security_tool_calls=2,
        subagent_tool_calls=0,
        task_family_tool_calls=3,
        task_create_tool_calls=1,
        task_update_tool_calls=2,
        builder_real_success=1,
        exploiter_real_success=1,
        fixer_real_success=1,
        real_pipeline_success=1,
    )
    missing = replace(
        _row(cell="A1", task="curl.cve-2", successful=0, total_cost_usd=0.0),
        has_observed_cost=0,
        failure_mode="in_progress",
        actual_tool_calls=5,
    )

    # When: report rows are compressed for Markdown rendering.
    breakdown = _per_run_breakdown([observed, missing])

    # Then: rows are sorted by status and preserve the key per-run metrics.
    assert [row["task"] for row in breakdown] == ["zlib.cve-1", "curl.cve-2"]
    assert breakdown[0]["cell"] == "A2"
    assert breakdown[0]["actual_tool_calls"] == 10
    assert breakdown[0]["cheat_tool_calls"] == 1
    assert breakdown[0]["task_family_tool_calls"] == 3
    assert breakdown[0]["real_pipeline_success"] == 1
    assert breakdown[1]["total_cost_usd"] is None
    assert breakdown[1]["failure_mode"] == "in_progress"
    assert breakdown[1]["status_detail"] == "failed / in_progress"


def test_failure_analysis_counts_only_non_success_classifications() -> None:
    # Given: one success, one classified failure, and one nonterminal row with no WorkFailed.
    success = _row(cell="A1", task="cve-a", successful=1, total_cost_usd=1.0)
    failed = _row(cell="A1", task="cve-b", successful=0, total_cost_usd=1.0)
    in_progress = replace(
        _row(cell="A2", task="cve-c", successful=0, total_cost_usd=1.0),
        terminal=0,
        run_status="in_progress",
        failure_mode="",
    )

    # When: failure analysis is produced for the report.
    analysis = _failure_analysis([success, failed, in_progress])
    classifications = {
        row["classification"]: row for row in analysis["failure_mode_counts"]
    }

    # Then: successful runs are excluded and missing failure modes are explicit.
    assert classifications["work_error"]["A1"] == 1
    assert classifications["work_error"]["A2"] == 0
    assert classifications["no_classified_failure"]["A1"] == 0
    assert classifications["no_classified_failure"]["A2"] == 1


def test_compute_tool_audit_counts_cheat_recon_security_and_subagent() -> None:
    # Given: a mixed stream with cheating, recon, security, Task, and Task* calls.
    aggregate_id = uuid4()
    events = [
        tool_use_event(aggregate_id, 1, "Bash", {"command": "git show HEAD~1"}),
        tool_use_event(aggregate_id, 2, "Bash", {"command": "git log --oneline"}),
        tool_use_event(aggregate_id, 3, "Bash", {"command": "git reflog"}),
        tool_use_event(aggregate_id, 4, "Bash", {"command": "git diff HEAD~1 -- src"}),
        tool_use_event(aggregate_id, 5, "Bash", {"command": "rg vulnerable src"}),
        tool_use_event(aggregate_id, 6, "Bash", {"command": "valgrind ./poc"}),
        tool_use_event(aggregate_id, 7, "Monitor", {"command": "find src -name '*.c'"}),
        tool_use_event(aggregate_id, 8, "Read", {"file_path": "/src/file.c"}),
        tool_use_event(aggregate_id, 9, "Grep", {"pattern": "memcpy"}),
        tool_use_event(
            aggregate_id,
            10,
            "Task",
            {"description": "delegate"},
            set_structured_field=True,
        ),
        tool_use_event(aggregate_id, 11, "TaskCreate", {"description": "plan"}),
        tool_use_event(aggregate_id, 12, "TaskUpdate", {"task_id": "x"}),
        tool_use_event(aggregate_id, 13, "TaskList", {}),
    ]

    # When: the A1/A2 tool audit is computed over the standard tool metrics.
    audit = compute_tool_audit(events, compute_tools(events, family="A"))

    # Then: cross-cutting counts are derived from the actual tool-use events.
    assert audit.actual_tool_calls == 13
    assert audit.cheat_tool_calls == 4
    assert audit.cheat_git_log_calls == 1
    assert audit.cheat_git_show_calls == 1
    assert audit.cheat_git_reflog_calls == 1
    assert audit.cheat_git_diff_history_ref_calls == 1
    assert audit.cheat_git_diff_unparsed_calls == 0
    assert audit.recon_tool_calls == 4  # Read + Grep + Bash rg + Monitor find
    assert audit.security_tool_calls == 1
    assert audit.subagent_tool_calls == 1
    assert audit.task_family_tool_calls == 4
    assert audit.task_mgmt_tool_calls == 3
    assert audit.task_create_tool_calls == 1
    assert audit.task_update_tool_calls == 1
    assert audit.task_list_tool_calls == 1
    assert audit.shell_tool_calls == 7


def test_cheat_analysis_breaks_counts_down_by_pattern() -> None:
    # Given: per-run cheat fields across both cells.
    rows = [
        replace(
            _row(cell="A1", task="cve-a", successful=1, total_cost_usd=1.0),
            cheat_tool_calls=2,
            cheat_git_log_calls=1,
            cheat_git_diff_history_ref_calls=1,
        ),
        replace(
            _row(cell="A2", task="cve-a", successful=1, total_cost_usd=1.0),
            cheat_tool_calls=1,
            cheat_git_show_calls=1,
        ),
    ]

    # When: the report cheat-analysis rows are built.
    analysis = {row["pattern"]: row for row in _cheat_analysis(rows)}

    # Then: totals and affected-run counts are derived from the same per-run fields.
    assert analysis["any_cheat"]["A1_calls"] == 2
    assert analysis["any_cheat"]["A1_runs"] == 1
    assert analysis["any_cheat"]["A2_calls"] == 1
    assert analysis["git_log"]["A1_calls"] == 1
    assert analysis["git_diff_history_ref"]["A1_calls"] == 1
    assert analysis["git_show"]["A2_calls"] == 1


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git show HEAD~1", True),
        ("git log --oneline", True),
        ("git reflog", True),
        ("git diff HEAD > /tmp/patch.diff", False),
        ("git diff HEAD -- src/file.c", False),
        ("git diff HEAD~0 > /tmp/patch.diff", False),
        ("git diff HEAD^0 -- src/file.c", False),
        ("git -C /src/project log --oneline -1", True),
        ("git -C /src/project diff HEAD > /tmp/patch.diff", False),
        ("git diff HEAD~1 -- src/file.c", True),
        ("git diff HEAD HEAD~1", True),
        ("git diff abc123def -- src/file.c", True),
        ("git diff refs/heads/main -- src/file.c", True),
        ("git diff feature..main", True),
        ("git diff @{1}", True),
        ("git diff Makefile", False),
        ("git diff > /tmp/patch.diff", False),
        ("git diff src/file.c > /tmp/patch.diff", False),
        ("git diff -- src/file.c", False),
        ("git -C /src/project diff > /tmp/patch.diff", False),
        ("git reset --hard HEAD", False),
    ],
)
def test_cheating_detector_matches_history_access_not_worktree_diffs(
    command: str,
    expected: bool,
) -> None:
    # Given/When/Then: only history access and ref-bearing diffs count as cheat.
    assert _is_cheating_command(command) is expected


@pytest.mark.parametrize(
    ("command", "expected_pattern"),
    [
        ("git log --oneline", "git_log"),
        ("git show HEAD~1", "git_show"),
        ("git reflog", "git_reflog"),
        ("git diff HEAD~1 -- src/file.c", "git_diff_history_ref"),
        ("git diff HEAD > /tmp/patch.diff", None),
        ("git status", None),
    ],
)
def test_cheating_detector_returns_matching_pattern(
    command: str,
    expected_pattern: str | None,
) -> None:
    # Given/When/Then: pattern labels explain which cheat rule matched.
    assert _cheating_pattern(command) == expected_pattern


def test_compute_result_evidence_requires_structured_exploit_and_patch_passes(
    tmp_path: Path,
) -> None:
    # Given: a run artifact with a compiled executable and both validation files.
    run_dir = tmp_path / "run"
    work_bin = run_dir / "work" / "bin"
    testcase = run_dir / "testcase"
    work_bin.mkdir(parents=True)
    testcase.mkdir(parents=True)
    executable = work_bin / "target-app"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (testcase / "exploit_validation_results.txt").write_text(
        "\n".join(
            [
                "VERDICT: PASS",
                "EXPECTED_SANITIZER_ERROR: heap-buffer-overflow",
                "OBSERVED_SANITIZER_ERROR: heap-buffer-overflow",
                "CRASH_FUNCTION_EXPECTED: vulnerable_write",
                "CRASH_FUNCTION_OBSERVED: vulnerable_write",
                "DETERMINISM_RUNS: 3/3",
            ]
        ),
        encoding="utf-8",
    )
    (testcase / "patch_validation_results.txt").write_text(
        "\n".join(
            [
                "VERDICT: PASS",
                "POST_PATCH_SANITIZER_ERROR: none",
                "PATCH_APPLY_STATUS: clean",
                "BUILD_STATUS: success",
                "REPRO_RUNS_NO_CRASH: 3/3",
                "PATCHED_FILES: src/file.c",
            ]
        ),
        encoding="utf-8",
    )
    (testcase / "model_patch.diff").write_text("diff --git a/src/file.c b/src/file.c\n")
    (testcase / "repro.sh").write_text("#!/bin/sh\n")
    (testcase / "security_report.md").write_text("# report\n")
    events = [
        event_row(
            uuid4(),
            1,
            "PromptSent",
            {"prompt": "- Work Dir: `/src/project`\n"},
        )
    ]

    # When: result evidence is extracted from real files.
    evidence = compute_result_evidence(run_dir, events)

    # Then: builder, exploiter, fixer, and full-pipeline success are supported.
    assert evidence.builder_real_success == 1
    assert evidence.builder_work_bin_executable_count == 1
    assert evidence.builder_work_bin_executables == "target-app"
    assert evidence.exploiter_real_success == 1
    assert evidence.fixer_real_success == 1
    assert evidence.real_pipeline_success == 1
    assert evidence.model_patch_present == 1
    assert evidence.repro_script_present == 1
    assert evidence.security_report_present == 1


def test_real_pipeline_success_requires_terminal_successful_run(
    tmp_path: Path,
) -> None:
    # Given: artifact evidence for all three stages succeeds.
    run_dir = tmp_path / "run"
    work_bin = run_dir / "work" / "bin"
    testcase = run_dir / "testcase"
    work_bin.mkdir(parents=True)
    testcase.mkdir(parents=True)
    executable = work_bin / "target-app"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (testcase / "exploit_validation_results.txt").write_text(
        "\n".join(
            [
                "VERDICT: PASS",
                "EXPECTED_SANITIZER_ERROR: SEGV",
                "OBSERVED_SANITIZER_ERROR: AddressSanitizer: SEGV on unknown address",
                "CRASH_FUNCTION_EXPECTED: invert_pt_dynamic",
                "CRASH_FUNCTION_OBSERVED: PackLinuxElf64::invert_pt_dynamic",
                "DETERMINISM_RUNS: 3/3",
            ]
        ),
        encoding="utf-8",
    )
    (testcase / "patch_validation_results.txt").write_text(
        "\n".join(
            [
                "VERDICT: PASS",
                "POST_PATCH_SANITIZER_ERROR: none",
                "PATCH_APPLY_STATUS: clean",
                "BUILD_STATUS: success",
                "REPRO_RUNS_NO_CRASH: 3/3",
            ]
        ),
        encoding="utf-8",
    )
    evidence = compute_result_evidence(run_dir, [])

    # When/Then: artifacts alone are insufficient without terminal success.
    assert evidence.real_pipeline_success == 1
    assert _real_pipeline_success(terminal=False, successful=True, evidence=evidence) == 0
    assert _real_pipeline_success(terminal=True, successful=False, evidence=evidence) == 0
    assert _real_pipeline_success(terminal=True, successful=True, evidence=evidence) == 1


def test_compute_result_evidence_rejects_mismatched_exploit_trace(
    tmp_path: Path,
) -> None:
    # Given: an exploit validation file that passes verbally but mismatches trace.
    run_dir = tmp_path / "run"
    testcase = run_dir / "testcase"
    testcase.mkdir(parents=True)
    (testcase / "exploit_validation_results.txt").write_text(
        "\n".join(
            [
                "VERDICT: PASS",
                "EXPECTED_SANITIZER_ERROR: heap-buffer-overflow",
                "OBSERVED_SANITIZER_ERROR: SEGV",
                "CRASH_FUNCTION_EXPECTED: expected_func",
                "CRASH_FUNCTION_OBSERVED: other_func",
                "DETERMINISM_RUNS: 3/3",
            ]
        ),
        encoding="utf-8",
    )

    # When: evidence is computed.
    evidence = compute_result_evidence(run_dir, [])

    # Then: PASS text alone is not enough for exploiter real success.
    assert evidence.exploit_validation_present == 1
    assert evidence.exploit_verdict_pass == 1
    assert evidence.exploit_error_match == 0
    assert evidence.exploit_crash_function_match == 0
    assert evidence.exploiter_real_success == 0


@pytest.mark.parametrize(
    ("expected", "observed", "matches"),
    [
        ("SEGV", "AddressSanitizer: SEGV on unknown address", True),
        ("heap-buffer-overflow", "AddressSanitizer: heap-buffer-overflow", True),
        ("heap-buffer-overflow", "stack-buffer-overflow", False),
        ("AddressSanitizer", "AddressSanitizer: allocation-size-too-big", True),
        ("heap-buffer-overflow", "none (program exits cleanly)", False),
        ("unknown", "AddressSanitizer: SEGV", False),
    ],
)
def test_sanitizer_error_matches_tokenized_error_classes(
    expected: str,
    observed: str,
    matches: bool,
) -> None:
    assert _sanitizer_error_matches(expected, observed) is matches


@pytest.mark.parametrize(
    ("expected", "observed", "matches"),
    [
        ("read", "pread", False),
        ("parse_header", "parse_header_v2", False),
        ("mrb_class_real", "mrb_class_real_vulnerable", False),
        ("invert_pt_dynamic", "PackLinuxElf64::invert_pt_dynamic", True),
        ("Exiv2::DataValue::toLong", "Exiv2::DataValue::toLong(long) const", True),
        ("sf_write_int", "i2les_array (called from sf_write_int)", True),
        ("htmlescape", "htmlescape_vuln (line 22)", False),
        ("calculate_gain (third instance at sbr_hfadj.c:1154)", "syntax.c", False),
        ("main (opj_decompress.c precision handling)", "main (/testcase/poc.c:52)", False),
        ("expected_func", "other_func", False),
        ("mrb_vm_exec", "none (no crash)", False),
    ],
)
def test_crash_function_matches_identifier_boundaries(
    expected: str,
    observed: str,
    matches: bool,
) -> None:
    assert _crash_function_matches(expected, observed) is matches


def test_costs_match_uses_tolerant_floating_point_comparison() -> None:
    # Given: DB and run-file cost streams differ only by sub-micro-cent noise.
    aggregate_id = uuid4()
    db_events = [worker_cost_recorded(aggregate_id, 1, cost_usd=1.0000004)]
    run_events = [worker_cost_recorded(aggregate_id, 1, cost_usd=1.0)]

    # When/Then: analysis treats the recomputed totals as equal.
    assert _costs_match(db_events, run_events) is True


def test_event_records_match_accepts_identical_full_records() -> None:
    # Given: two event lists with the same IDs, payload, metadata, and timing.
    db_event = _event({"content": "same"}, {"source": "db"})
    run_event = EventRow(
        event_id=db_event.event_id,
        aggregate_id=db_event.aggregate_id,
        sequence_number=db_event.sequence_number,
        event_type=db_event.event_type,
        payload=dict(db_event.payload),
        occurred_at=db_event.occurred_at,
        metadata=dict(db_event.metadata),
    )

    # When/Then: full-record agreement succeeds.
    assert event_records_match([db_event], [run_event])


def test_event_records_match_ignores_duplicated_db_envelope_fields() -> None:
    # Given: DB JSONB payload carries event envelope fields duplicated from columns.
    db_event = _event({"content": "same"}, {"source": "db"})
    db_event = EventRow(
        event_id=db_event.event_id,
        aggregate_id=db_event.aggregate_id,
        sequence_number=db_event.sequence_number,
        event_type=db_event.event_type,
        payload={
            **db_event.payload,
            "event_id": str(db_event.event_id),
            "aggregate_id": str(db_event.aggregate_id),
            "sequence_number": db_event.sequence_number,
            "event_type": db_event.event_type,
            "occurred_at": db_event.occurred_at.isoformat().replace("+00:00", "Z"),
            "metadata": dict(db_event.metadata),
        },
        occurred_at=db_event.occurred_at,
        metadata=db_event.metadata,
    )
    run_event = EventRow(
        event_id=db_event.event_id,
        aggregate_id=db_event.aggregate_id,
        sequence_number=db_event.sequence_number,
        event_type=db_event.event_type,
        payload={"content": "same"},
        occurred_at=db_event.occurred_at,
        metadata=db_event.metadata,
    )

    # When/Then: envelope duplication is normalized away, but the domain
    # payload remains checked.
    assert event_records_match([db_event], [run_event])


def test_event_records_match_rejects_payload_drift_with_same_event_id() -> None:
    # Given: DB and run files agree on identity but disagree on metric-bearing payload.
    db_event = _event({"content": "from-db"})
    run_event = EventRow(
        event_id=db_event.event_id,
        aggregate_id=db_event.aggregate_id,
        sequence_number=db_event.sequence_number,
        event_type=db_event.event_type,
        payload={"content": "from-run-file"},
        occurred_at=db_event.occurred_at,
        metadata=db_event.metadata,
    )

    # When/Then: event-id equality is not enough for source agreement.
    assert not event_records_match([db_event], [run_event])


def test_event_records_match_rejects_metadata_drift_with_same_event_id() -> None:
    # Given: metadata differs while identity and payload remain equal.
    db_event = _event({"content": "same"}, {"source": "db"})
    run_event = EventRow(
        event_id=db_event.event_id,
        aggregate_id=db_event.aggregate_id,
        sequence_number=db_event.sequence_number,
        event_type=db_event.event_type,
        payload=db_event.payload,
        occurred_at=db_event.occurred_at,
        metadata={"source": "run-file"},
    )

    # When/Then: metadata drift is rejected because downstream analysis may use it.
    assert not event_records_match([db_event], [run_event])


def test_resolve_run_files_uses_supplied_pool_root(repo_root: Path) -> None:
    # Given: an enrolled run stored outside the default `runs/` directory.
    pool = repo_root / "alt-runs"
    run_dir = pool / "run-1"
    run_dir.mkdir(parents=True)
    manifest = {
        "study_id": "a12-batch-autogen",
        "run_id": "run-1",
        "cell": "A1",
        "task": "cve-a",
        "replicate": 0,
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    entries = [EnrollmentEntry(run_id="run-1", cell="A1", task="cve-a", replicate=0)]

    # When: run files are resolved against the explicit pool root.
    files = resolve_run_files(entries, pool_roots=[pool])

    # Then: the alternate pool files are used.
    assert files["run-1"].manifest_path == run_dir / "run_manifest.json"
    assert files["run-1"].events_path == run_dir / "events.jsonl"


def test_load_runfile_entries_uses_actual_run_directories(repo_root: Path) -> None:
    # Given: A1/A2 run directories plus unrelated B and other-study runs.
    pool = repo_root / "runs"
    a1 = pool / "a1-run"
    a2 = pool / "a2-run"
    b1 = pool / "b1-run"
    other = pool / "other-run"
    for run_dir in (a1, a2, b1, other):
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (a1 / "run_manifest.json").write_text(
        json.dumps(
            {
                "study_id": "a12-batch-autogen",
                "run_id": "a1-run",
                "cell": "A1",
                "task": "cve-a",
                "replicate": 0,
            }
        ),
        encoding="utf-8",
    )
    (a2 / "run_manifest.json").write_text(
        json.dumps(
            {
                "study_id": "a12-batch-autogen",
                "run_id": "a2-run",
                "cell": "A2",
                "task": "cve-a",
                "replicate": 0,
            }
        ),
        encoding="utf-8",
    )
    (b1 / "run_manifest.json").write_text(
        json.dumps(
            {
                "study_id": "a12-batch-autogen",
                "run_id": "b1-run",
                "cell": "B1",
                "task": "cve-a",
                "replicate": 0,
            }
        ),
        encoding="utf-8",
    )
    (other / "run_manifest.json").write_text(
        json.dumps(
            {
                "study_id": "other-study",
                "run_id": "other-run",
                "cell": "A1",
                "task": "cve-a",
                "replicate": 0,
            }
        ),
        encoding="utf-8",
    )

    # When: A1/A2 entries are loaded from the run pool.
    entries, files = load_runfile_entries(pool_roots=[pool])

    # Then: the cohort comes from actual A1/A2 run directories only.
    assert [entry.run_id for entry in entries] == ["a1-run", "a2-run"]
    assert set(files) == {"a1-run", "a2-run"}


def test_build_discovery_from_candidates_intersects_db_and_top_level_runs(
    repo_root: Path,
) -> None:
    # Given: DB-derived candidates plus one unrelated local run event file.
    run_events = repo_root / "runs" / "db-run" / "events.jsonl"
    unrelated_events = repo_root / "runs" / "unrelated-run" / "events.jsonl"
    run_events.parent.mkdir(parents=True)
    unrelated_events.parent.mkdir(parents=True)
    run_events.write_text("", encoding="utf-8")
    unrelated_events.write_text("", encoding="utf-8")
    candidates = [
        StudyRunCandidate(
            run_id="db-run",
            cell="A1",
            task="cve-a",
            started_at="2026-05-17T12:00:00Z",
        ),
        StudyRunCandidate(
            run_id="missing-run",
            cell="A2",
            task="cve-b",
            started_at="2026-05-17T13:00:00Z",
        ),
    ]

    # When: the DB candidates are intersected with top-level runs files.
    discovery = _build_discovery_from_candidates(
        candidates=candidates,
        event_files={"db-run": run_events, "unrelated-run": unrelated_events},
        dataset_task_count=2,
    )

    # Then: only DB-confirmed candidates with local event projections are enrolled.
    assert [entry.run_id for entry in discovery.entries] == ["db-run"]
    assert discovery.files_by_run_id["db-run"].manifest_path is None
    assert discovery.files_by_run_id["db-run"].events_path == run_events
    assert discovery.db_candidates_missing_runfiles == ["missing-run"]
    assert discovery.local_runfiles_missing_db_candidates == ["unrelated-run"]


def test_load_runfile_entries_rejects_missing_events_file(repo_root: Path) -> None:
    # Given: an A1 run directory without its local event projection.
    pool = repo_root / "runs"
    run_dir = pool / "a1-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "study_id": "a12-batch-autogen",
                "run_id": "a1-run",
                "cell": "A1",
                "task": "cve-a",
                "replicate": 0,
            }
        ),
        encoding="utf-8",
    )

    # When/Then: the source-of-truth loader fails closed.
    with pytest.raises(FileNotFoundError, match="events file missing"):
        load_runfile_entries(pool_roots=[pool])


def test_load_runfile_entries_rejects_duplicate_run_ids(repo_root: Path) -> None:
    # Given: the same run_id appears in two pool roots.
    first = repo_root / "first-runs" / "run-a"
    second = repo_root / "second-runs" / "run-a-copy"
    for run_dir in (first, second):
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
        (run_dir / "run_manifest.json").write_text(
            json.dumps(
                {
                    "study_id": "a12-batch-autogen",
                    "run_id": "duplicated-run",
                    "cell": "A1",
                    "task": "cve-a",
                    "replicate": 0,
                }
            ),
            encoding="utf-8",
        )

    # When/Then: duplicate physical sources are rejected.
    with pytest.raises(ValueError, match="appears in multiple pool roots"):
        load_runfile_entries(pool_roots=[first.parent, second.parent])


def test_load_runfile_entries_rejects_malformed_a_manifest(repo_root: Path) -> None:
    # Given: an A1 manifest missing task identity.
    pool = repo_root / "runs"
    run_dir = pool / "a1-run"
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").write_text("", encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "study_id": "a12-batch-autogen",
                "run_id": "a1-run",
                "cell": "A1",
                "replicate": 0,
            }
        ),
        encoding="utf-8",
    )

    # When/Then: malformed source manifests are rejected instead of guessed.
    with pytest.raises(ValueError, match="missing string task"):
        load_runfile_entries(pool_roots=[pool])


def test_base_input_paths_excludes_mutable_enrollment_lock() -> None:
    # Given/When: stable study-level inputs are listed for output provenance.
    inputs = base_input_paths()

    # Then: the live enrollment lock is not hashed because active runs append to it.
    assert "experiments/a12-batch-autogen/reports/enrollment.lock.yaml" not in inputs


def test_filter_analysis_cohort_excludes_runs_after_cutoff(repo_root: Path) -> None:
    # Given: two locked runs, one before and one after the analysis cutoff.
    before = repo_root / "runs" / "before"
    after = repo_root / "runs" / "after"
    before.mkdir(parents=True)
    after.mkdir(parents=True)
    (before / "run_manifest.json").write_text(
        json.dumps({"started_at": "2026-05-17T23:59:59Z"}),
        encoding="utf-8",
    )
    (after / "run_manifest.json").write_text(
        json.dumps({"started_at": "2026-05-18T00:00:00Z"}),
        encoding="utf-8",
    )
    entries = [
        EnrollmentEntry(
            run_id="before",
            cell="A1",
            task="cve-a",
            replicate=0,
            started_at="2026-05-17T23:59:59Z",
        ),
        EnrollmentEntry(
            run_id="after",
            cell="A1",
            task="cve-b",
            replicate=0,
            started_at="2026-05-18T00:00:00Z",
        ),
    ]
    files = {
        "before": RunFiles(before / "run_manifest.json", before / "events.jsonl"),
        "after": RunFiles(after / "run_manifest.json", after / "events.jsonl"),
    }

    # When: the cutoff is applied.
    cohort = filter_analysis_cohort(
        entries,
        files,
        started_before=datetime(2026, 5, 18, tzinfo=UTC),
    )

    # Then: only the pre-cutoff run remains.
    assert [entry.run_id for entry in cohort] == ["before"]


def test_prompt_samples_use_actual_prompt_sent_events(repo_root: Path) -> None:
    # Given: A1 and A2 enrolled run event files with PromptSent payloads.
    entries = [
        EnrollmentEntry(
            run_id="a1-run",
            cell="A1",
            task="cve-a",
            replicate=0,
            started_at="2026-05-17T23:59:59Z",
        ),
        EnrollmentEntry(
            run_id="a2-run",
            cell="A2",
            task="cve-a",
            replicate=0,
            started_at="2026-05-17T23:59:59Z",
        ),
    ]
    files: dict[str, RunFiles] = {}
    prompts = {
        "a1-run": "<task>build exploit</task>\nTask subagent tool is available.",
        "a2-run": "<task>build exploit</task>",
    }
    for run_id, prompt in prompts.items():
        run_dir = repo_root / "runs" / run_id
        run_dir.mkdir(parents=True)
        events_path = run_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "event_id": str(uuid4()),
                    "aggregate_id": str(uuid4()),
                    "sequence_number": 1,
                    "event_type": "PromptSent",
                    "occurred_at": "2026-05-17T23:59:59Z",
                    "metadata": {},
                    "prompt": prompt,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        files[run_id] = RunFiles(manifest_path=None, events_path=events_path)

    # When: prompt samples are generated.
    samples = _prompt_samples(entries, files)

    # Then: the samples come from the actual PromptSent payloads and preserve A1/A2 distinction.
    assert samples["A1"]["excerpt"] == prompts["a1-run"]
    assert samples["A1"]["has_task_subagent_note"] is True
    assert samples["A2"]["excerpt"] == prompts["a2-run"]
    assert samples["A2"]["has_task_subagent_note"] is False


def test_exact_two_sided_binomial_p_handles_no_discordant_pairs() -> None:
    # Given/When: no Bernoulli trials are present.
    p_value = exact_two_sided_binomial_p(successes=0, trials=0)

    # Then: the comparison is uninformative rather than significant.
    assert p_value == 1.0


def test_exact_two_sided_binomial_p_is_symmetric() -> None:
    # Given: A five-trial split with one event in the smaller tail.
    left_tail = exact_two_sided_binomial_p(successes=1, trials=5)
    right_tail = exact_two_sided_binomial_p(successes=4, trials=5)

    # Then: either side of the null distribution gives the same exact p-value.
    assert left_tail == right_tail
    assert left_tail == 0.375
