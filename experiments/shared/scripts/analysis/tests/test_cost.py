"""Unit tests for ``experiments.shared.scripts.analysis.metrics.cost``."""

from __future__ import annotations

from uuid import uuid4

import pytest
from hypothesis import given, settings, strategies as st

from experiments.shared.scripts.analysis.metrics.cost import compute_cost
from experiments.shared.scripts.analysis.tests.factories import (
    event_row,
    tokens_consumed,
    worker_cost_recorded,
)


def test_compute_cost_happy_path_mixes_models_operations_and_worker_cost() -> None:
    # Given: two TokensConsumed events (different models and operations)
    # plus one WorkerCostRecorded carrying cache + reasoning tokens
    boss = uuid4()
    events = [
        tokens_consumed(
            boss,
            1,
            model="claude-sonnet",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            cost_usd=0.01,
            operation="task_decomposition",
        ),
        tokens_consumed(
            boss,
            2,
            model="gpt-4o",
            prompt_tokens=200,
            completion_tokens=80,
            total_tokens=280,
            cost_usd=0.04,
            operation="worker_execution",
        ),
        worker_cost_recorded(
            boss,
            3,
            cost_usd=0.05,
            prompt_tokens=999,
            completion_tokens=999,
            cache_read_tokens=10,
            cache_write_tokens=5,
            reasoning_tokens=20,
        ),
    ]

    # When: compute_cost aggregates them
    cost = compute_cost(events)

    # Then: manager, worker, and total cost are separately auditable
    assert cost.manager_cost_usd == 0.05
    assert cost.worker_cost_usd == 0.05
    assert cost.total_cost_usd == 0.1
    assert cost.total_llm_cost_usd == 0.05
    assert cost.total_worker_cost_usd == 0.05
    assert cost.prompt_tokens == 1299
    assert cost.completion_tokens == 1129
    assert cost.total_tokens == 2463
    assert cost.cache_read_tokens == 10  # WorkerCostRecorded only
    assert cost.cache_write_tokens == 5
    assert cost.reasoning_tokens == 20
    assert cost.llm_call_count == 2
    assert cost.manager_cost_by_model == {"claude-sonnet": 0.01, "gpt-4o": 0.04}
    assert cost.worker_cost_by_model == {"claude-sonnet": 0.05}
    assert cost.cost_by_model == pytest.approx({"claude-sonnet": 0.06, "gpt-4o": 0.04})
    assert cost.cost_by_operation == {
        "task_decomposition": 0.01,
        "worker_execution": 0.04,
    }
    # Self-check: model totals include both channels; operations are manager-only.
    assert sum(cost.cost_by_model.values()) == pytest.approx(cost.total_cost_usd)
    assert sum(cost.cost_by_operation.values()) == pytest.approx(cost.manager_cost_usd)


def test_compute_cost_empty_input_zeros_everything() -> None:
    # Given: no events
    events = []

    # When: compute_cost runs over an empty stream
    cost = compute_cost(events)

    # Then: every numeric field is zero and the breakdown dicts are empty
    assert cost.manager_cost_usd == 0.0
    assert cost.worker_cost_usd == 0.0
    assert cost.total_cost_usd == 0.0
    assert cost.total_llm_cost_usd == 0.0
    assert cost.total_worker_cost_usd == 0.0
    assert cost.total_tokens == 0
    assert cost.prompt_tokens == 0
    assert cost.completion_tokens == 0
    assert cost.cache_read_tokens == 0
    assert cost.cache_write_tokens == 0
    assert cost.reasoning_tokens == 0
    assert cost.llm_call_count == 0
    assert cost.manager_cost_by_model == {}
    assert cost.worker_cost_by_model == {}
    assert cost.cost_by_model == {}
    assert cost.cost_by_operation == {}


def test_compute_cost_groups_multiple_tokens_consumed_with_same_model() -> None:
    # Given: three TokensConsumed events sharing one model but split across operations
    boss = uuid4()
    events = [
        tokens_consumed(
            boss,
            1,
            model="claude-sonnet",
            cost_usd=0.02,
            operation="complexity_evaluation",
        ),
        tokens_consumed(
            boss,
            2,
            model="claude-sonnet",
            cost_usd=0.03,
            operation="task_decomposition",
        ),
        tokens_consumed(
            boss,
            3,
            model="claude-sonnet",
            cost_usd=0.05,
            operation="task_decomposition",
        ),
    ]

    # When: compute_cost aggregates them
    cost = compute_cost(events)

    # Then: by_model collapses to one key while by_operation keeps two
    assert cost.manager_cost_by_model == {"claude-sonnet": 0.1}
    assert cost.worker_cost_by_model == {}
    assert cost.cost_by_model == {"claude-sonnet": 0.1}
    assert cost.cost_by_operation == {
        "complexity_evaluation": 0.02,
        "task_decomposition": 0.08,
    }
    assert cost.llm_call_count == 3


def test_compute_cost_worker_cost_recorded_populates_worker_cost_and_tokens() -> None:
    # Given: a sole WorkerCostRecorded event with cache + reasoning tokens
    boss = uuid4()
    events = [
        worker_cost_recorded(
            boss,
            1,
            cost_usd=0.12,
            prompt_tokens=400,
            completion_tokens=100,
            cache_read_tokens=70,
            cache_write_tokens=30,
            reasoning_tokens=40,
        ),
    ]

    # When: compute_cost aggregates them
    cost = compute_cost(events)

    # Then: worker cost and worker token usage are captured without manager cost.
    assert cost.manager_cost_usd == 0.0
    assert cost.worker_cost_usd == 0.12
    assert cost.total_cost_usd == 0.12
    assert cost.total_worker_cost_usd == 0.12
    assert cost.total_llm_cost_usd == 0.0
    assert cost.cache_read_tokens == 70
    assert cost.cache_write_tokens == 30
    assert cost.reasoning_tokens == 40
    assert cost.prompt_tokens == 400
    assert cost.completion_tokens == 100
    assert cost.total_tokens == 640
    assert cost.llm_call_count == 0
    assert cost.manager_cost_by_model == {}
    assert cost.worker_cost_by_model == {"claude-sonnet": 0.12}
    assert cost.cost_by_model == {"claude-sonnet": 0.12}
    assert cost.cost_by_operation == {}


def test_compute_cost_worker_usage_metrics_override_top_level_model_cost() -> None:
    # Given: worker cost with detailed SDK usage metrics for two model entries
    boss = uuid4()
    worker = worker_cost_recorded(
        boss,
        1,
        model="top-level-model",
        cost_usd=0.30,
    )
    payload = {
        **worker.payload,
        "usage_metrics": [
            {"model": "claude-sonnet", "accumulated_cost_usd": 0.10},
            {"model": "claude-sonnet", "accumulated_cost_usd": 0.05},
            {"model": "gpt-4o", "accumulated_cost_usd": 0.15},
        ],
    }
    events = [event_row(boss, 1, "WorkerCostRecorded", payload)]

    # When: compute_cost aggregates worker model attribution
    cost = compute_cost(events)

    # Then: usage_metrics provide the worker model breakdown
    assert cost.worker_cost_usd == 0.30
    assert cost.worker_cost_by_model == pytest.approx({"claude-sonnet": 0.15, "gpt-4o": 0.15})
    assert cost.cost_by_model == cost.worker_cost_by_model


def test_compute_cost_worker_usage_metrics_skip_invalid_entries_before_valid() -> None:
    # Given: usage_metrics with malformed and unattributed entries before valid cost data
    boss = uuid4()
    worker = worker_cost_recorded(
        boss,
        1,
        model="top-level-model",
        cost_usd=0.20,
    )
    payload = {
        **worker.payload,
        "usage_metrics": [
            ["not", "a", "mapping"],
            {"accumulated_cost_usd": 99.0},
            {"model": "", "accumulated_cost_usd": 99.0},
            {"model": "claude-sonnet", "accumulated_cost_usd": 0.20},
            {"model": "gpt-4o"},
        ],
    }
    events = [event_row(boss, 1, "WorkerCostRecorded", payload)]

    # When: compute_cost reads the usage metrics
    cost = compute_cost(events)

    # Then: invalid entries are skipped and zero-cost entries do not create attribution
    assert cost.worker_cost_by_model == {"claude-sonnet": 0.20}
    assert cost.cost_by_model == {"claude-sonnet": 0.20}


def test_compute_cost_accumulates_multiple_worker_cost_records() -> None:
    # Given: two worker cost records with the same model and non-zero token buckets
    boss = uuid4()
    events = [
        worker_cost_recorded(
            boss,
            1,
            cost_usd=0.04,
            prompt_tokens=1,
            completion_tokens=2,
            cache_read_tokens=3,
            cache_write_tokens=4,
            reasoning_tokens=5,
        ),
        worker_cost_recorded(
            boss,
            2,
            cost_usd=0.06,
            prompt_tokens=10,
            completion_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
            reasoning_tokens=50,
        ),
    ]

    # When: compute_cost aggregates repeated worker-side records
    cost = compute_cost(events)

    # Then: worker cost, token buckets, and model attribution are additive
    assert cost.worker_cost_usd == 0.10
    assert cost.total_cost_usd == 0.10
    assert cost.prompt_tokens == 11
    assert cost.completion_tokens == 22
    assert cost.cache_read_tokens == 33
    assert cost.cache_write_tokens == 44
    assert cost.reasoning_tokens == 55
    assert cost.total_tokens == 165
    assert cost.worker_cost_by_model == pytest.approx({"claude-sonnet": 0.10})


def test_compute_cost_worker_tokens_fall_back_to_tokens_when_breakdown_missing() -> None:
    # Given: older worker cost data with only a top-level tokens field
    boss = uuid4()
    events = [
        event_row(
            boss,
            1,
            "WorkerCostRecorded",
            {
                "tool_name": "claude_code",
                "model": "claude-sonnet",
                "tokens": 321,
                "cost_usd": 0.03,
            },
        ),
    ]

    # When: compute_cost aggregates the older event shape
    cost = compute_cost(events)

    # Then: total_tokens still includes the worker usage
    assert cost.total_tokens == 321
    assert cost.worker_cost_usd == 0.03


@pytest.mark.property
@given(
    manager_costs=st.lists(
        st.floats(
            min_value=0.0,
            max_value=100.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        max_size=20,
    ),
    worker_costs=st.lists(
        st.floats(
            min_value=0.0,
            max_value=100.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        max_size=20,
    ),
)
@settings(max_examples=50, deadline=None)
def test_compute_cost_total_cost_is_additive(
    manager_costs: list[float],
    worker_costs: list[float],
) -> None:
    # Given: arbitrary non-negative manager and worker cost events
    boss = uuid4()
    events = [
        tokens_consumed(boss, index + 1, cost_usd=cost)
        for index, cost in enumerate(manager_costs)
    ]
    offset = len(events) + 1
    events.extend(
        worker_cost_recorded(boss, offset + index, cost_usd=cost)
        for index, cost in enumerate(worker_costs)
    )

    # When: compute_cost aggregates the event stream
    cost = compute_cost(events)

    # Then: total_cost_usd always equals manager plus worker cost
    assert cost.manager_cost_usd == pytest.approx(sum(manager_costs))
    assert cost.worker_cost_usd == pytest.approx(sum(worker_costs))
    assert cost.total_cost_usd == pytest.approx(
        cost.manager_cost_usd + cost.worker_cost_usd
    )
