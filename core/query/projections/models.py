"""Immutable data models for the projection pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from core.domain.events import DomainEvent


@dataclass(frozen=True)
class CostSummary:
    """Aggregated cost statistics from TokensConsumed and WorkerCostRecorded events."""

    total_cost_usd: float
    llm_cost_usd: float
    worker_cost_usd: float
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cost_by_model: dict[str, float] = field(default_factory=dict)
    cost_by_operation: dict[str, float] = field(default_factory=dict)
    cost_by_agent: dict[str, float] = field(default_factory=dict)
    budget_limit_usd: float | None = None
    budget_remaining_usd: float | None = None
    budget_exceeded: bool = False

    @classmethod
    def empty(cls, budget_limit: float | None = None) -> "CostSummary":
        """Create empty cost summary with zero values."""
        return cls(
            total_cost_usd=0.0,
            llm_cost_usd=0.0,
            worker_cost_usd=0.0,
            total_tokens=0,
            prompt_tokens=0,
            completion_tokens=0,
            cost_by_model={},
            cost_by_operation={},
            cost_by_agent={},
            budget_limit_usd=budget_limit,
            budget_remaining_usd=budget_limit,
            budget_exceeded=False,
        )


@dataclass(frozen=True)
class ProjectionSummary:
    """Aggregated event statistics from a projection run."""

    total_events: int
    events_by_type: dict[str, int]
    agents_involved: frozenset[UUID]
    first_event: datetime | None
    last_event: datetime | None
    error_count: int
    errors: tuple[DomainEvent, ...]

    @classmethod
    def empty(cls) -> "ProjectionSummary":
        """Create empty summary with zero counts."""
        return cls(
            total_events=0,
            events_by_type={},
            agents_involved=frozenset(),
            first_event=None,
            last_event=None,
            error_count=0,
            errors=(),
        )
