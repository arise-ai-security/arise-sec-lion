"""Tests for CostProjection."""

from datetime import timedelta

import pytest

from core.domain.events.events import AgentCreated, WorkerCostRecorded, WorkerUsageMetrics
from core.query.projections.impl.cost import CostProjection
from core.query.tests.projections.conftest import BASE_TIME, WORKER_ID


class TestCostProjection:
    """Tests for detailed worker cost aggregation."""

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
