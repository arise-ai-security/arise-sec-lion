"""Unit tests for ``metrics.timing`` (design doc §5)."""

from __future__ import annotations

from uuid import uuid4

from experiments.shared.scripts.analysis.metrics.timing import compute_timing
from experiments.shared.scripts.analysis.tests.factories import (
    _BASE_TIME,
    agent_execution_finished,
    operation_finished,
    run_completed,
    run_started,
)


def test_happy_path_computes_all_fields():
    # Given: a boss aggregate with RunStarted at t=0, RunCompleted at t=42s,
    # two AgentExecutionFinished events totaling 30s, and two OperationFinished
    # events with distinct operation_types totaling 15s.
    run_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        agent_execution_finished(run_id, 2, duration_seconds=20.0, offset_ms=1_000),
        agent_execution_finished(run_id, 3, duration_seconds=10.0, offset_ms=2_000),
        operation_finished(
            run_id, 4, operation_type="worker_execution", duration_seconds=10.0,
            offset_ms=3_000,
        ),
        operation_finished(
            run_id, 5, operation_type="task_decomposition", duration_seconds=5.0,
            offset_ms=4_000,
        ),
        run_completed(run_id, 6, duration_seconds=42.0, offset_ms=42_000),
    ]

    # When: timing is computed
    result = compute_timing(events, run_id=run_id)

    # Then: every field reflects the inputs
    assert result.run_started_at == _BASE_TIME
    assert result.run_completed_at is not None
    assert result.wall_clock_seconds == 42.0
    assert result.run_duration_seconds == 42.0
    assert result.agent_execution_total_seconds == 30.0
    assert result.operation_total_seconds == 15.0
    assert result.operation_seconds_by_type == {
        "worker_execution": 10.0,
        "task_decomposition": 5.0,
    }


def test_missing_run_completed_leaves_boss_fields_partial():
    # Given: only RunStarted on the boss, plus an AgentExecutionFinished event
    run_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        agent_execution_finished(run_id, 2, duration_seconds=7.5, offset_ms=1_000),
    ]

    # When: timing is computed
    result = compute_timing(events, run_id=run_id)

    # Then: run_started_at is set; completion-derived fields are None;
    # totals are still aggregated from the events that do exist.
    assert result.run_started_at == _BASE_TIME
    assert result.run_completed_at is None
    assert result.wall_clock_seconds is None
    assert result.run_duration_seconds is None
    assert result.agent_execution_total_seconds == 7.5
    assert result.operation_total_seconds == 0.0
    assert result.operation_seconds_by_type == {}


def test_missing_run_started_leaves_start_fields_none():
    # Given: only a RunCompleted event on the boss (no RunStarted)
    run_id = uuid4()
    events = [
        run_completed(run_id, 1, duration_seconds=12.0, offset_ms=12_000),
    ]

    # When: timing is computed
    result = compute_timing(events, run_id=run_id)

    # Then: run_started_at is None and wall_clock_seconds cannot be derived,
    # but run_duration_seconds is still recovered from the payload.
    assert result.run_started_at is None
    assert result.run_completed_at is not None
    assert result.wall_clock_seconds is None
    assert result.run_duration_seconds == 12.0


def test_multiple_operations_same_type_are_summed():
    # Given: three OperationFinished events of the same operation_type
    run_id = uuid4()
    events = [
        operation_finished(
            run_id, 1, operation_type="worker_execution", duration_seconds=3.0,
            offset_ms=100,
        ),
        operation_finished(
            run_id, 2, operation_type="worker_execution", duration_seconds=4.0,
            offset_ms=200,
        ),
        operation_finished(
            run_id, 3, operation_type="worker_execution", duration_seconds=5.0,
            offset_ms=300,
        ),
    ]

    # When: timing is computed
    result = compute_timing(events, run_id=run_id)

    # Then: per-type and total sums match across the three events
    assert result.operation_seconds_by_type == {"worker_execution": 12.0}
    assert result.operation_total_seconds == 12.0


def test_empty_events_yields_zero_defaults():
    # Given: no events at all
    run_id = uuid4()

    # When: timing is computed
    result = compute_timing([], run_id=run_id)

    # Then: every field is at its empty/zero default
    assert result.run_started_at is None
    assert result.run_completed_at is None
    assert result.wall_clock_seconds is None
    assert result.run_duration_seconds is None
    assert result.agent_execution_total_seconds == 0.0
    assert result.operation_total_seconds == 0.0
    assert result.operation_seconds_by_type == {}


def test_agent_execution_finished_on_non_boss_aggregate_is_counted():
    # Given: an AgentExecutionFinished event emitted on a non-boss aggregate
    # (e.g. a child worker) alongside the boss's own AgentExecutionFinished
    run_id = uuid4()
    worker_id = uuid4()
    events = [
        run_started(run_id, 1, offset_ms=0),
        agent_execution_finished(run_id, 2, duration_seconds=8.0, offset_ms=1_000),
        agent_execution_finished(worker_id, 1, duration_seconds=5.0, offset_ms=1_500),
        run_completed(run_id, 3, duration_seconds=10.0, offset_ms=10_000),
    ]

    # When: timing is computed
    result = compute_timing(events, run_id=run_id)

    # Then: the non-boss event's duration is included in the cross-aggregate total
    assert result.agent_execution_total_seconds == 13.0
    assert result.run_duration_seconds == 10.0
    assert result.wall_clock_seconds == 10.0
