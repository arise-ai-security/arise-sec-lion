"""Immutable data models for the projection pipeline."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from core.domain.events import DomainEvent


@dataclass(frozen=True)
class AgentListItem:
    """Lightweight read model for agent listing.

    This is a CQRS read model that can be built directly from events
    without reconstructing the full AgentSession aggregate.
    """

    agent_id: UUID
    role: str
    status: str
    task_description: str | None
    parent_id: UUID | None
    created_at: datetime | None
    child_ids: tuple[UUID, ...] = field(default_factory=tuple)

    @classmethod
    def empty(cls, agent_id: UUID) -> "AgentListItem":
        """Create empty agent list item."""
        return cls(
            agent_id=agent_id,
            role="PENDING",
            status="pending",
            task_description=None,
            parent_id=None,
            created_at=None,
            child_ids=(),
        )


@dataclass(frozen=True)
class NodeCountSummary:
    """Agent node counts by role."""

    total: int
    by_role: dict[str, int] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "NodeCountSummary":
        """Create empty node count summary."""
        return cls(total=0, by_role={})


@dataclass(frozen=True)
class ExecutionTimeSummary:
    """Execution time breakdown from event timestamps."""

    total_seconds: float
    per_role: dict[str, float] = field(default_factory=dict)
    per_phase: dict[str, float] = field(default_factory=dict)
    per_agent: dict[str, float] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ExecutionTimeSummary":
        """Create empty execution time summary."""
        return cls(total_seconds=0.0, per_role={}, per_phase={}, per_agent={})


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
    cost_by_role: dict[str, float] = field(default_factory=dict)
    tokens_by_role: dict[str, int] = field(default_factory=dict)
    budget_limit_usd: float | None = None
    budget_remaining_usd: float | None = None
    budget_exceeded: bool = False

    def __post_init__(self) -> None:
        """Validate cost invariants after construction."""
        self._validate_invariants()

    def _validate_invariants(self) -> None:
        """Verify all cost invariants hold.

        Raises:
            CostInvariantViolation: If any invariant is violated.
        """
        from core.domain.exceptions import CostInvariantViolation

        tolerance = 1e-6

        # Invariant 1: total_cost = llm_cost + worker_cost
        expected_total = self.llm_cost_usd + self.worker_cost_usd
        if abs(self.total_cost_usd - expected_total) > tolerance:
            raise CostInvariantViolation(
                invariant="total_cost == llm_cost + worker_cost",
                expected=expected_total,
                actual=self.total_cost_usd,
            )

        # Invariant 2: sum(cost_by_operation) == total_cost
        if self.cost_by_operation:
            sum_by_operation = sum(self.cost_by_operation.values())
            if abs(sum_by_operation - self.total_cost_usd) > tolerance:
                raise CostInvariantViolation(
                    invariant="sum(cost_by_operation) == total_cost",
                    expected=self.total_cost_usd,
                    actual=sum_by_operation,
                )

        # Invariant 3: sum(cost_by_agent) == total_cost
        if self.cost_by_agent:
            sum_by_agent = sum(self.cost_by_agent.values())
            if abs(sum_by_agent - self.total_cost_usd) > tolerance:
                raise CostInvariantViolation(
                    invariant="sum(cost_by_agent) == total_cost",
                    expected=self.total_cost_usd,
                    actual=sum_by_agent,
                )

        # Invariant 4: sum(cost_by_role) == total_cost
        if self.cost_by_role:
            sum_by_role = sum(self.cost_by_role.values())
            if abs(sum_by_role - self.total_cost_usd) > tolerance:
                raise CostInvariantViolation(
                    invariant="sum(cost_by_role) == total_cost",
                    expected=self.total_cost_usd,
                    actual=sum_by_role,
                )

        # Invariant 5: all costs are non-negative
        if self.total_cost_usd < 0 or self.llm_cost_usd < 0 or self.worker_cost_usd < 0:
            raise CostInvariantViolation(
                invariant="all_costs >= 0",
                expected="non-negative",
                actual=f"total={self.total_cost_usd}, llm={self.llm_cost_usd}, "
                f"worker={self.worker_cost_usd}",
            )

        # Invariant 6: budget_remaining consistency
        if self.budget_limit_usd is not None and self.budget_remaining_usd is not None:
            expected_remaining = max(0.0, self.budget_limit_usd - self.total_cost_usd)
            if abs(self.budget_remaining_usd - expected_remaining) > tolerance:
                raise CostInvariantViolation(
                    invariant="budget_remaining == max(0, budget_limit - total_cost)",
                    expected=expected_remaining,
                    actual=self.budget_remaining_usd,
                )

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
            cost_by_role={},
            tokens_by_role={},
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
    node_counts: NodeCountSummary | None = None
    cost: CostSummary | None = None
    execution_time: ExecutionTimeSummary | None = None

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
            node_counts=None,
            cost=None,
            execution_time=None,
        )


@dataclass(frozen=True)
class SubtaskSummary:
    """Read model for a subtask with optional child agent info."""

    description: str
    child_id: UUID | None = None
    child_status: str | None = None


@dataclass(frozen=True)
class AgentSummary:
    """Lightweight read model for agent summary projection.

    This is a CQRS read model that aggregates data from multiple events
    to provide a comprehensive view of an agent's state and configuration.
    """

    agent_id: UUID
    role: str
    status: str
    task_description: str

    # Complexity evaluation (from ComplexityEvaluated event)
    complexity: str | None = None
    complexity_reasoning: str | None = None

    # For WORKER agents (from CodeGenerationStarted event)
    worker_tool: str | None = None

    # For MANAGER agents (from SubtasksDefined event)
    subtasks: tuple[SubtaskSummary, ...] = field(default_factory=tuple)

    # Configuration
    config_strategy: str | None = None
    config_details: dict = field(default_factory=dict)

    # Result/Error
    result: str | None = None
    error_message: str | None = None
