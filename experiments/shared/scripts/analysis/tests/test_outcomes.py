"""Tests for ``compute_outcomes`` and ``classify_failure``.

Covers every documented ``failure_mode`` branch (design doc §8 / §13.4),
boss-vs-all-aggregates discipline for counts, verification grouping, and
the empty-events degenerate case.
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

from experiments.shared.scripts.analysis.metrics.outcomes import (
    classify_failure,
    compute_outcomes,
)
from experiments.shared.scripts.analysis.tests.factories import (
    decision_infeasible,
    redecomposition_triggered,
    retry_scheduled,
    run_completed,
    run_started,
    verification_failed,
    verification_passed,
    work_completed,
    work_failed,
)


# ---------------------------------------------------------------------------
# classify_failure — exhaustive branches
# ---------------------------------------------------------------------------


def test_classify_failure_hard_timeout_from_reason():
    # Given: a WorkFailed reason matching the hard-timeout format
    reason = "Timed out after 5400s"

    # When: classify_failure is called without exit_status
    result = classify_failure(reason, None)

    # Then: it reports a hard timeout
    assert result == "hard_timeout"


def test_classify_failure_inactivity_timeout_from_reason():
    # Given: a WorkFailed reason matching the inactivity-timeout format
    reason = "Timed out after 600s without Claude output"

    # When: classify_failure is called without exit_status
    result = classify_failure(reason, None)

    # Then: it reports an inactivity timeout
    assert result == "inactivity_timeout"


def test_classify_failure_provider_failure_from_reason():
    # Given: a reason that mentions a provider error
    reason = "LLM provider returned 429"

    # When: classify_failure is called
    result = classify_failure(reason, None)

    # Then: it reports a provider failure
    assert result == "provider_failure"


def test_classify_failure_generic_work_error_from_reason():
    # Given: a reason that matches no known timeout/provider pattern
    reason = "some other error"

    # When: classify_failure is called
    result = classify_failure(reason, None)

    # Then: it falls back to generic work_error
    assert result == "work_error"


def test_classify_failure_exit_status_timeout_with_inactivity_reason():
    # Given: exit_status="timeout" plus a reason that names inactivity
    reason = "Timed out after 600s without Claude output"

    # When: classify_failure is called
    result = classify_failure(reason, "timeout")

    # Then: the inactivity branch wins
    assert result == "inactivity_timeout"


def test_classify_failure_exit_status_timeout_with_hard_reason():
    # Given: exit_status="timeout" plus a hard-timeout reason
    reason = "Timed out after 5400s"

    # When: classify_failure is called
    result = classify_failure(reason, "timeout")

    # Then: the hard-timeout branch wins
    assert result == "hard_timeout"


def test_classify_failure_exit_status_timeout_no_reason():
    # Given: exit_status="timeout" but no reason text
    # When: classify_failure is called
    result = classify_failure(None, "timeout")

    # Then: hard-timeout is the default for an unannotated timeout
    assert result == "hard_timeout"


def test_classify_failure_exit_status_non_success_no_reason():
    # Given: a non-success exit_status and no reason
    # When: classify_failure is called
    result = classify_failure(None, "killed")

    # Then: it falls back to work_error
    assert result == "work_error"


def test_classify_failure_returns_none_for_clean_finish():
    # Given: no reason and a successful exit_status
    # When: classify_failure is called
    result = classify_failure(None, "success")

    # Then: there is no failure
    assert result is None


def test_classify_failure_returns_none_for_empty_inputs():
    # Given: no reason and no exit_status
    # When: classify_failure is called
    result = classify_failure(None, None)

    # Then: there is no failure
    assert result is None


# ---------------------------------------------------------------------------
# compute_outcomes — failure-mode propagation through full event sequences
# ---------------------------------------------------------------------------


def test_compute_outcomes_hard_timeout_from_work_failed_reason():
    # Given: a boss with a hard-timeout WorkFailed event
    boss = uuid4()
    events = [
        run_started(boss, 1),
        work_failed(boss, 2, reason="Timed out after 5400s"),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: failure_mode and reason match, run_status remains in_progress
    assert outcomes.failure_mode == "hard_timeout"
    assert outcomes.failure_reason == "Timed out after 5400s"
    assert outcomes.has_work_failed is True
    assert outcomes.work_failed_count == 1
    assert outcomes.run_status == "in_progress"
    assert outcomes.has_run_completed is False


def test_compute_outcomes_inactivity_timeout_from_work_failed_reason():
    # Given: a boss with an inactivity-timeout WorkFailed
    boss = uuid4()
    events = [
        run_started(boss, 1),
        work_failed(boss, 2, reason="Timed out after 600s without Claude output"),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: inactivity branch wins
    assert outcomes.failure_mode == "inactivity_timeout"
    assert outcomes.failure_reason == "Timed out after 600s without Claude output"


def test_compute_outcomes_provider_failure_from_work_failed_reason():
    # Given: a boss with a provider failure WorkFailed
    boss = uuid4()
    events = [
        run_started(boss, 1),
        work_failed(boss, 2, reason="LLM provider returned 429"),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: classified as provider_failure
    assert outcomes.failure_mode == "provider_failure"
    assert outcomes.failure_reason == "LLM provider returned 429"


def test_compute_outcomes_generic_work_error():
    # Given: a boss WorkFailed with an unrecognised reason
    boss = uuid4()
    events = [
        run_started(boss, 1),
        work_failed(boss, 2, reason="some other error"),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: classified as a generic work_error
    assert outcomes.failure_mode == "work_error"
    assert outcomes.failure_reason == "some other error"


def test_compute_outcomes_completed_run_has_no_failure_mode():
    # Given: a clean run with WorkCompleted + RunCompleted(status="completed")
    boss = uuid4()
    events = [
        run_started(boss, 1),
        work_completed(boss, 2),
        run_completed(boss, 3, status="completed"),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: no failure mode and run_status follows RunCompleted
    assert outcomes.failure_mode is None
    assert outcomes.failure_reason is None
    assert outcomes.run_status == "completed"
    assert outcomes.has_run_completed is True
    assert outcomes.has_work_completed is True
    assert outcomes.has_work_failed is False
    assert outcomes.work_completed_count == 1
    assert outcomes.work_failed_count == 0
    assert outcomes.worker_exit_status is None


def test_compute_outcomes_exit_status_timeout_with_inactivity_reason():
    # Given: a WorkFailed whose metadata carries exit_status="timeout"
    boss = uuid4()
    work_fail = work_failed(
        boss, 2, reason="Timed out after 600s without Claude output",
    )
    work_fail = dataclasses.replace(work_fail, metadata={"exit_status": "timeout"})
    events = [run_started(boss, 1), work_fail]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: failure_mode is inactivity_timeout and exit_status is surfaced
    assert outcomes.failure_mode == "inactivity_timeout"
    assert outcomes.worker_exit_status == "timeout"


def test_compute_outcomes_exit_status_timeout_with_hard_reason():
    # Given: exit_status="timeout" but the reason is hard-timeout
    boss = uuid4()
    work_fail = work_failed(boss, 2, reason="Timed out after 5400s")
    work_fail = dataclasses.replace(work_fail, metadata={"exit_status": "timeout"})
    events = [run_started(boss, 1), work_fail]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: failure_mode is hard_timeout
    assert outcomes.failure_mode == "hard_timeout"
    assert outcomes.worker_exit_status == "timeout"


def test_compute_outcomes_multi_aggregate_counts_roll_up_but_failure_mode_is_boss_only():
    # Given: a boss WorkFailed with a generic reason plus child WorkFailed/Completed
    # events that carry timeout reasons. Counts span all aggregates but
    # failure_mode must reflect only the boss's own WorkFailed.
    boss = uuid4()
    child_a = uuid4()
    child_b = uuid4()
    events = [
        run_started(boss, 1),
        work_failed(boss, 2, reason="some other error"),
        work_completed(child_a, 1),
        work_failed(child_b, 1, reason="Timed out after 5400s"),
        work_failed(child_b, 2, reason="Timed out after 600s without Claude output"),
    ]

    # When: outcomes are computed against the boss run_id
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: counts include children but failure_mode follows the boss reason
    assert outcomes.work_failed_count == 3
    assert outcomes.work_completed_count == 1
    assert outcomes.has_work_failed is True
    assert outcomes.has_work_completed is False
    assert outcomes.failure_mode == "work_error"
    assert outcomes.failure_reason == "some other error"


def test_compute_outcomes_verification_pass_rate_and_failed_stages():
    # Given: 2 VerificationPassed + 1 VerificationFailed with a known failed_stage
    boss = uuid4()
    child = uuid4()
    events = [
        run_started(boss, 1),
        verification_passed(boss, 2),
        verification_passed(child, 1),
        verification_failed(child, 2, failed_stage="judge"),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: pass-rate is 2/3 and failed_stages groups by failed_stage
    assert outcomes.verification_passed_count == 2
    assert outcomes.verification_failed_count == 1
    assert outcomes.verification_pass_rate is not None
    assert abs(outcomes.verification_pass_rate - (2 / 3)) < 1e-9
    assert outcomes.verification_failed_stages == {"judge": 1}


def test_compute_outcomes_retries_redecompositions_decisions():
    # Given: retry / redecomposition / decision-infeasible events across aggregates
    boss = uuid4()
    child = uuid4()
    events = [
        run_started(boss, 1),
        retry_scheduled(boss, 2),
        retry_scheduled(child, 1),
        redecomposition_triggered(boss, 3, trigger_child_id=child),
        decision_infeasible(child, 2),
    ]

    # When: outcomes are computed
    outcomes = compute_outcomes(events, run_id=boss)

    # Then: every counter rolls up across all aggregates
    assert outcomes.retries == 2
    assert outcomes.redecompositions == 1
    assert outcomes.decisions_infeasible == 1


def test_compute_outcomes_empty_events_is_in_progress():
    # Given: no events at all
    boss = uuid4()

    # When: outcomes are computed
    outcomes = compute_outcomes([], run_id=boss)

    # Then: every count is zero, every bool is False, run_status is in_progress
    assert outcomes.run_status == "in_progress"
    assert outcomes.has_run_completed is False
    assert outcomes.has_work_completed is False
    assert outcomes.has_work_failed is False
    assert outcomes.work_completed_count == 0
    assert outcomes.work_failed_count == 0
    assert outcomes.verification_passed_count == 0
    assert outcomes.verification_failed_count == 0
    assert outcomes.verification_pass_rate is None
    assert outcomes.verification_failed_stages == {}
    assert outcomes.retries == 0
    assert outcomes.redecompositions == 0
    assert outcomes.decisions_infeasible == 0
    assert outcomes.failure_mode is None
    assert outcomes.failure_reason is None
    assert outcomes.worker_exit_status is None
