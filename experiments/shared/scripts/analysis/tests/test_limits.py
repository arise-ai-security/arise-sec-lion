"""Unit tests for ``metrics.limits`` (design doc §5)."""

from __future__ import annotations

from uuid import uuid4

from experiments.shared.scripts.analysis.metrics.limits import compute_limits
from experiments.shared.scripts.analysis.tests.factories import (
    event_row,
    limit_enforced,
    run_started,
    work_completed,
)


def test_empty_input_returns_zero_counts():
    # Given: an empty event sequence
    events = []
    # When: compute_limits runs
    result = compute_limits(events)
    # Then: total is zero and the breakdown is empty
    assert result.total_enforcements == 0
    assert result.by_limit_type == {}


def test_single_limit_enforced_depth():
    # Given: a single LimitEnforced event of type "depth"
    aggregate_id = uuid4()
    events = [limit_enforced(aggregate_id, 1, limit_type="depth")]
    # When: compute_limits runs
    result = compute_limits(events)
    # Then: total is 1 and the breakdown reflects one depth enforcement
    assert result.total_enforcements == 1
    assert result.by_limit_type == {"depth": 1}


def test_multiple_distinct_types_some_doubled():
    # Given: four LimitEnforced events across four distinct limit types,
    #   with two types ("depth", "children") appearing twice
    aggregate_id = uuid4()
    events = [
        limit_enforced(aggregate_id, 1, limit_type="depth"),
        limit_enforced(aggregate_id, 2, limit_type="depth"),
        limit_enforced(aggregate_id, 3, limit_type="total_agents"),
        limit_enforced(aggregate_id, 4, limit_type="children"),
        limit_enforced(aggregate_id, 5, limit_type="children"),
        limit_enforced(aggregate_id, 6, limit_type="agents"),
    ]
    # When: compute_limits runs
    result = compute_limits(events)
    # Then: total counts every event and the breakdown reflects per-type counts
    assert result.total_enforcements == 6
    assert result.by_limit_type == {
        "depth": 2,
        "total_agents": 1,
        "children": 2,
        "agents": 1,
    }


def test_repeated_same_type_accumulates():
    # Given: three LimitEnforced events all of type "depth"
    aggregate_id = uuid4()
    events = [
        limit_enforced(aggregate_id, 1, limit_type="depth"),
        limit_enforced(aggregate_id, 2, limit_type="depth"),
        limit_enforced(aggregate_id, 3, limit_type="depth"),
    ]
    # When: compute_limits runs
    result = compute_limits(events)
    # Then: total and per-type counts both equal 3
    assert result.total_enforcements == 3
    assert result.by_limit_type == {"depth": 3}


def test_missing_limit_type_categorized_as_unknown():
    # Given: a LimitEnforced event whose payload omits limit_type (None)
    aggregate_id = uuid4()
    events = [
        event_row(
            aggregate_id,
            1,
            "LimitEnforced",
            {
                "limit_type": None,
                "limit_value": 3,
                "attempted_value": 4,
                "action_taken": "rejected_children",
            },
        ),
    ]
    # When: compute_limits runs
    result = compute_limits(events)
    # Then: it is counted and bucketed under "unknown"
    assert result.total_enforcements == 1
    assert result.by_limit_type == {"unknown": 1}


def test_non_limit_enforced_events_ignored():
    # Given: a mixed sequence with two non-LimitEnforced events and one LimitEnforced
    aggregate_id = uuid4()
    events = [
        run_started(aggregate_id, 1),
        limit_enforced(aggregate_id, 2, limit_type="total_agents"),
        work_completed(aggregate_id, 3),
    ]
    # When: compute_limits runs
    result = compute_limits(events)
    # Then: only the LimitEnforced event contributes
    assert result.total_enforcements == 1
    assert result.by_limit_type == {"total_agents": 1}
