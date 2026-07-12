"""Tests for CostProjection."""

from datetime import timedelta

import pytest

from core.domain.events.events import (
    AgentCreated,
    TokensConsumed,
    WorkerCostRecorded,
    WorkerUsageMetrics,
)
from core.query.projections.impl.cost import CostProjection
from core.query.tests.projections.conftest import BASE_TIME, WORKER_ID


class TestCostProjection:
    """Tests for detailed worker cost aggregation."""

    def test_sums_cache_tokens_from_tokens_consumed(self) -> None:
        # Given: a stream with a TokensConsumed event carrying cache tokens
        events = [
            AgentCreated(
                aggregate_id=WORKER_ID,
                sequence_number=1,
                role="WORKER",
                occurred_at=BASE_TIME,
            ),
            TokensConsumed(
                aggregate_id=WORKER_ID,
                sequence_number=2,
                model="claude-3-5-sonnet",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cache_read_tokens=8,
                cache_write_tokens=3,
                cost_usd=0.01,
                operation="assess",
            ),
        ]

        # When: projecting cost
        result = CostProjection().project(events)

        # Then: cache tokens from TokensConsumed are accumulated
        assert result.cache_read_tokens == 8
        assert result.cache_write_tokens == 3

    def test_aggregates_detailed_worker_metrics(self) -> None:
        events = [
            AgentCreated(
                aggregate_id=WORKER_ID,
                sequence_number=1,
                role="WORKER",
                occurred_at=BASE_TIME,
            ),
            WorkerCostRecorded(
                aggregate_id=WORKER_ID,
                sequence_number=2,
                tool_name="openhands",
                model="openai/gpt-4o",
                tokens=21,
                prompt_tokens=10,
                completion_tokens=5,
                cache_read_tokens=2,
                cache_write_tokens=1,
                reasoning_tokens=3,
                usage_metrics=[
                    WorkerUsageMetrics(
                        usage_id="primary",
                        model="openai/gpt-4o",
                        accumulated_cost_usd=0.04,
                    ),
                    WorkerUsageMetrics(
                        usage_id="helper",
                        model="openai/gpt-4o-mini",
                        accumulated_cost_usd=0.08,
                    ),
                ],
                cost_usd=0.12,
                duration_seconds=18.0,
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
        ]

        result = CostProjection().project(events)

        assert result.total_cost_usd == pytest.approx(0.12)
        assert result.worker_cost_usd == pytest.approx(0.12)
        assert result.total_tokens == 21
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 5
        assert result.cache_read_tokens == 2
        assert result.cache_write_tokens == 1
        assert result.reasoning_tokens == 3
        assert result.cost_by_model["openai/gpt-4o"] == pytest.approx(0.04)
        assert result.cost_by_model["openai/gpt-4o-mini"] == pytest.approx(0.08)

    def test_timeout_usage_is_explicitly_incomplete(self) -> None:
        # Given: A timeout record whose provider usage could not be recovered
        events = [
            WorkerCostRecorded(
                aggregate_id=WORKER_ID,
                sequence_number=1,
                tool_name="openhands",
                complete=False,
                termination_reason="timeout",
                usage_missing=True,
            )
        ]

        # When: Cost completeness is projected
        result = CostProjection().project(events)

        # Then: Zero is not silently treated as complete usage
        assert result.cost_incomplete
        assert result.worker_usage_records == 1
        assert result.complete_worker_usage_records == 0
        assert result.cost_completeness_rate == 0.0
