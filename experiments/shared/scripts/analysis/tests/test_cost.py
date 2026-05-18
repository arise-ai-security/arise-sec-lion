"""Unit tests for ``experiments.shared.scripts.analysis.metrics.cost``."""

from __future__ import annotations

from uuid import uuid4

from experiments.shared.scripts.analysis.metrics.cost import compute_cost
from experiments.shared.scripts.analysis.tests.factories import (
    tokens_consumed,
    worker_cost_recorded,
)


def test_compute_cost_happy_path_mixes_models_operations_and_worker_cost():
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
            prompt_tokens=999,  # must NOT be summed into prompt_tokens
            completion_tokens=999,  # must NOT be summed into completion_tokens
            cache_read_tokens=10,
            cache_write_tokens=5,
            reasoning_tokens=20,
        ),
    ]

    # When: compute_cost aggregates them
    cost = compute_cost(events)

    # Then: per-event-type accumulation is honored
    assert cost.total_llm_cost_usd == 0.05
    assert cost.total_worker_cost_usd == 0.05
    assert cost.prompt_tokens == 300  # TokensConsumed only
    assert cost.completion_tokens == 130  # TokensConsumed only
    assert cost.total_tokens == 430
    assert cost.cache_read_tokens == 10  # WorkerCostRecorded only
    assert cost.cache_write_tokens == 5
    assert cost.reasoning_tokens == 20
    assert cost.llm_call_count == 2
    assert cost.cost_by_model == {"claude-sonnet": 0.01, "gpt-4o": 0.04}
    assert cost.cost_by_operation == {
        "task_decomposition": 0.01,
        "worker_execution": 0.04,
    }
    # Self-check: cost_by_model totals to total_llm_cost_usd
    assert sum(cost.cost_by_model.values()) == cost.total_llm_cost_usd
    assert sum(cost.cost_by_operation.values()) == cost.total_llm_cost_usd


def test_compute_cost_empty_input_zeros_everything():
    # Given: no events
    events = []

    # When: compute_cost runs over an empty stream
    cost = compute_cost(events)

    # Then: every numeric field is zero and the breakdown dicts are empty
    assert cost.total_llm_cost_usd == 0.0
    assert cost.total_worker_cost_usd == 0.0
    assert cost.total_tokens == 0
    assert cost.prompt_tokens == 0
    assert cost.completion_tokens == 0
    assert cost.cache_read_tokens == 0
    assert cost.cache_write_tokens == 0
    assert cost.reasoning_tokens == 0
    assert cost.llm_call_count == 0
    assert cost.cost_by_model == {}
    assert cost.cost_by_operation == {}


def test_compute_cost_groups_multiple_tokens_consumed_with_same_model():
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
    assert cost.cost_by_model == {"claude-sonnet": 0.1}
    assert cost.cost_by_operation == {
        "complexity_evaluation": 0.02,
        "task_decomposition": 0.08,
    }
    assert cost.llm_call_count == 3


def test_compute_cost_worker_cost_recorded_only_populates_cache_and_reasoning():
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

    # Then: worker cost is captured; cache + reasoning tokens flow through;
    # TokensConsumed-sourced fields stay at zero so the per-event-type rule
    # does not silently double-count worker token usage.
    assert cost.total_worker_cost_usd == 0.12
    assert cost.total_llm_cost_usd == 0.0
    assert cost.cache_read_tokens == 70
    assert cost.cache_write_tokens == 30
    assert cost.reasoning_tokens == 40
    assert cost.prompt_tokens == 0
    assert cost.completion_tokens == 0
    assert cost.total_tokens == 0
    assert cost.llm_call_count == 0
    assert cost.cost_by_model == {}
    assert cost.cost_by_operation == {}
