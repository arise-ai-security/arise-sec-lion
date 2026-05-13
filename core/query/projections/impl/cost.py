"""Cost projection: aggregate token usage and costs from events."""

from collections import defaultdict
from collections.abc import Iterable

from core.domain.events.events import (
    DomainEvent,
    TokensConsumed,
    WorkerCostRecorded,
)
from core.query.projections.base import Projection
from core.query.projections.models import CostSummary
from core.query.projections.registry import register_projection


def _recorded_worker_tokens(event: WorkerCostRecorded) -> int:
    return event.total_recorded_tokens or 0


@register_projection("cost")
class CostProjection(Projection):
    """Aggregate cost events into summary statistics.

    Processes TokensConsumed and WorkerCostRecorded events to produce
    a comprehensive cost breakdown by model, operation, and agent.
    """

    def __init__(self, budget_limit_usd: float | None = None) -> None:
        """Initialize with optional budget limit for tracking.

        Args:
            budget_limit_usd: Budget limit in USD (for calculating remaining budget).
        """
        self._budget_limit = budget_limit_usd

    def project(self, events: Iterable[DomainEvent]) -> CostSummary:
        """Transform events into cost summary.

        Args:
            events: Iterable of domain events to process.

        Returns:
            CostSummary with aggregated cost statistics.
        """
        events_list = list(events)
        if not events_list:
            return CostSummary.empty(self._budget_limit)

        # Accumulators
        llm_cost = 0.0
        worker_cost = 0.0
        total_tokens = 0
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0
        cost_by_model: dict[str, float] = defaultdict(float)
        cost_by_operation: dict[str, float] = defaultdict(float)
        cost_by_agent: dict[str, float] = defaultdict(float)
        budget_exceeded = False

        for event in events_list:
            if isinstance(event, TokensConsumed):
                llm_cost += event.cost_usd
                total_tokens += event.total_tokens
                prompt_tokens += event.prompt_tokens
                completion_tokens += event.completion_tokens
                cost_by_model[event.model] += event.cost_usd
                cost_by_operation[event.operation] += event.cost_usd
                cost_by_agent[str(event.aggregate_id)] += event.cost_usd

            elif isinstance(event, WorkerCostRecorded):
                worker_cost += event.cost_usd
                for model_name, model_cost in event.model_costs.items():
                    cost_by_model[model_name] += model_cost
                operation_key = f"worker:{event.tool_name}"
                cost_by_operation[operation_key] += event.cost_usd
                cost_by_agent[str(event.aggregate_id)] += event.cost_usd
                total_tokens += _recorded_worker_tokens(event)
                prompt_tokens += event.prompt_tokens or 0
                completion_tokens += event.completion_tokens or 0
                cache_read_tokens += event.cache_read_tokens or 0
                cache_write_tokens += event.cache_write_tokens or 0
                reasoning_tokens += event.reasoning_tokens or 0

        total_cost = llm_cost + worker_cost

        budget_remaining = None
        if self._budget_limit is not None:
            budget_remaining = max(0.0, self._budget_limit - total_cost)
            if total_cost >= self._budget_limit:
                budget_exceeded = True

        return CostSummary(
            total_cost_usd=round(total_cost, 6),
            llm_cost_usd=round(llm_cost, 6),
            worker_cost_usd=round(worker_cost, 6),
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_by_model=dict(cost_by_model),
            cost_by_operation=dict(cost_by_operation),
            cost_by_agent=dict(cost_by_agent),
            budget_limit_usd=self._budget_limit,
            budget_remaining_usd=budget_remaining,
            budget_exceeded=budget_exceeded,
        )
