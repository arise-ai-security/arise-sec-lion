"""Summary projection: aggregate events into statistics."""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import singledispatchmethod
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.events.events import (
    AgentCreated,
    DecisionInfeasible,
    DomainEvent,
    OperationFinished,
    StatusChanged,
    TokensConsumed,
    VerificationFailed,
    WorkerCostRecorded,
    WorkFailed,
)
from core.query.projections.base import Projection
from core.query.projections.models import (
    CostSummary,
    ExecutionTimeSummary,
    NodeCountSummary,
    ProjectionSummary,
)
from core.query.projections.registry import register_projection


if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


def _recorded_worker_tokens(event: WorkerCostRecorded) -> int:
    return event.total_recorded_tokens or 0


class _SummaryAccumulator:
    """Single-pass event accumulator shared by the batch and incremental projections.

    Holds all summary state (counts, roles, costs, timing, errors) and folds
    one event at a time, so both projection front-ends share one copy of the
    accumulation logic instead of drifting duplicates.
    """

    def __init__(self, budget_limit_usd: float | None = None) -> None:
        self._budget_limit = budget_limit_usd
        self._total_events = 0
        self._events_by_type: Counter[str] = Counter()
        self._first_event: datetime | None = None
        self._last_event: datetime | None = None
        self._errors: list[DomainEvent] = []

        # Role tracking
        self._role_map: dict[UUID, str] = {}

        # Cost tracking
        self._llm_cost = 0.0
        self._worker_cost = 0.0
        self._total_tokens = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._reasoning_tokens = 0
        self._cost_by_model: dict[str, float] = defaultdict(float)
        self._cost_by_operation: dict[str, float] = defaultdict(float)
        self._cost_by_agent: dict[str, float] = defaultdict(float)
        self._cost_by_role: dict[str, float] = defaultdict(float)
        self._tokens_by_role: dict[str, int] = defaultdict(int)

        # Timing tracking
        self._agent_first: dict[UUID, datetime] = {}
        self._agent_last: dict[UUID, datetime] = {}
        self._phase_times: dict[str, float] = defaultdict(float)
        self._agent_phase_start: dict[tuple[UUID, str], datetime] = {}
        self._operation_times: dict[str, float] = defaultdict(float)

    def process(self, event: DomainEvent) -> None:
        """Fold one event into the accumulated state."""
        self._total_events += 1
        self._events_by_type[type(event).__name__] += 1

        # Track timestamps
        if self._first_event is None:
            self._first_event = event.occurred_at
        self._last_event = event.occurred_at

        # Track agent timestamps for timing
        agent_id = event.aggregate_id
        if agent_id not in self._agent_first:
            self._agent_first[agent_id] = event.occurred_at
        self._agent_last[agent_id] = event.occurred_at

        self._dispatch(event)

    @singledispatchmethod
    def _dispatch(self, event: DomainEvent) -> None:
        """Events with no type-specific metrics only update the common counters."""

    @_dispatch.register
    def _(self, event: AgentCreated) -> None:
        self._role_map[event.aggregate_id] = event.role.upper()

    @_dispatch.register
    def _(self, event: TokensConsumed) -> None:
        agent_id = event.aggregate_id
        role = self._role_map.get(agent_id, "UNKNOWN")
        self._llm_cost += event.cost_usd
        self._total_tokens += event.total_tokens
        self._prompt_tokens += event.prompt_tokens
        self._completion_tokens += event.completion_tokens
        self._cache_read_tokens += event.cache_read_tokens
        self._cache_write_tokens += event.cache_write_tokens
        self._cost_by_model[event.model] += event.cost_usd
        self._cost_by_operation[event.operation] += event.cost_usd
        self._cost_by_agent[str(agent_id)] += event.cost_usd
        self._cost_by_role[role] += event.cost_usd
        self._tokens_by_role[role] += event.total_tokens

    @_dispatch.register
    def _(self, event: WorkerCostRecorded) -> None:
        agent_id = event.aggregate_id
        role = self._role_map.get(agent_id, "UNKNOWN")
        self._worker_cost += event.cost_usd
        for model_name, model_cost in event.model_costs.items():
            self._cost_by_model[model_name] += model_cost
        operation_key = f"worker:{event.tool_name}"
        self._cost_by_operation[operation_key] += event.cost_usd
        self._cost_by_agent[str(agent_id)] += event.cost_usd
        self._cost_by_role[role] += event.cost_usd
        recorded_tokens = _recorded_worker_tokens(event)
        self._total_tokens += recorded_tokens
        self._prompt_tokens += event.prompt_tokens or 0
        self._completion_tokens += event.completion_tokens or 0
        self._cache_read_tokens += event.cache_read_tokens or 0
        self._cache_write_tokens += event.cache_write_tokens or 0
        self._reasoning_tokens += event.reasoning_tokens or 0
        self._tokens_by_role[role] += recorded_tokens

    @_dispatch.register
    def _(self, event: StatusChanged) -> None:
        agent_id = event.aggregate_id
        old_phase = event.old_status.lower()
        new_phase = event.new_status.lower()
        phase_key = (agent_id, old_phase)

        if phase_key in self._agent_phase_start:
            start_time = self._agent_phase_start[phase_key]
            duration = (event.occurred_at - start_time).total_seconds()
            self._phase_times[old_phase] += duration
            del self._agent_phase_start[phase_key]

        self._agent_phase_start[(agent_id, new_phase)] = event.occurred_at

    @_dispatch.register
    def _(self, event: WorkFailed) -> None:
        self._errors.append(event)

    @_dispatch.register
    def _(self, event: VerificationFailed) -> None:
        self._errors.append(event)

    @_dispatch.register
    def _(self, event: DecisionInfeasible) -> None:
        self._errors.append(event)

    @_dispatch.register
    def _(self, event: OperationFinished) -> None:
        self._operation_times[event.operation_type] += event.duration_seconds

    def build(self) -> ProjectionSummary:
        """Materialize the accumulated state into a ProjectionSummary."""
        if self._total_events == 0:
            return ProjectionSummary.empty()

        total_cost = self._llm_cost + self._worker_cost
        budget_remaining = None
        budget_exceeded = False

        if self._budget_limit is not None:
            budget_remaining = max(0.0, self._budget_limit - total_cost)
            budget_exceeded = total_cost >= self._budget_limit

        cost = CostSummary(
            total_cost_usd=round(total_cost, 6),
            llm_cost_usd=round(self._llm_cost, 6),
            worker_cost_usd=round(self._worker_cost, 6),
            total_tokens=self._total_tokens,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            cache_read_tokens=self._cache_read_tokens,
            cache_write_tokens=self._cache_write_tokens,
            reasoning_tokens=self._reasoning_tokens,
            cost_by_model={k: round(v, 6) for k, v in self._cost_by_model.items()},
            cost_by_operation={k: round(v, 6) for k, v in self._cost_by_operation.items()},
            cost_by_agent={k: round(v, 6) for k, v in self._cost_by_agent.items()},
            cost_by_role={k: round(v, 6) for k, v in self._cost_by_role.items()},
            tokens_by_role=dict(self._tokens_by_role),
            budget_limit_usd=self._budget_limit,
            budget_remaining_usd=(
                round(budget_remaining, 6) if budget_remaining is not None else None
            ),
            budget_exceeded=budget_exceeded,
        )

        per_agent: dict[str, float] = {}
        for agent_id in self._agent_first:
            first = self._agent_first[agent_id]
            last = self._agent_last[agent_id]
            per_agent[str(agent_id)] = (last - first).total_seconds()

        per_role: dict[str, float] = defaultdict(float)
        for agent_id_str, duration in per_agent.items():
            uuid_id = UUID(agent_id_str)
            role = self._role_map.get(uuid_id, "UNKNOWN")
            per_role[role] += duration

        total_seconds = 0.0
        if self._first_event and self._last_event:
            total_seconds = (self._last_event - self._first_event).total_seconds()

        timing = ExecutionTimeSummary(
            total_seconds=total_seconds,
            per_role=dict(per_role),
            per_phase=dict(self._phase_times),
            per_agent=per_agent,
            per_operation=dict(self._operation_times),
        )

        by_role: dict[str, int] = defaultdict(int)
        for role in self._role_map.values():
            by_role[role] += 1

        node_counts = NodeCountSummary(
            total=len(self._role_map),
            by_role=dict(by_role),
        )

        return ProjectionSummary(
            total_events=self._total_events,
            events_by_type=dict(self._events_by_type),
            agents_involved=frozenset(self._role_map.keys()),
            first_event=self._first_event,
            last_event=self._last_event,
            error_count=len(self._errors),
            errors=tuple(self._errors),
            node_counts=node_counts,
            cost=cost,
            execution_time=timing,
        )


class IncrementalSummaryProjection:
    """Incremental summary projection that maintains state between updates.

    Instead of re-processing all events on each update, this accumulates
    state and only processes new events. O(new_events) instead of O(all_events).
    """

    def __init__(self, budget_limit_usd: float | None = None) -> None:
        self._accumulator = _SummaryAccumulator(budget_limit_usd)

    def update(self, new_events: list[DomainEvent]) -> ProjectionSummary:
        """Process new events and return updated summary.

        Only processes the new events, not the entire history.
        """
        for event in new_events:
            self._accumulator.process(event)

        return self._accumulator.build()


@register_projection("summary")
class SummaryProjection(Projection):
    """Aggregate events into counts, timestamps, costs, timing, and node counts."""

    def __init__(self, budget_limit_usd: float | None = None) -> None:
        """Initialize the projection.

        Args:
            budget_limit_usd: Optional budget limit for budget tracking.
        """
        self._budget_limit = budget_limit_usd

    def project(self, events: Iterable[DomainEvent]) -> ProjectionSummary:
        """Project events into a comprehensive summary.

        Args:
            events: Domain events to aggregate.

        Returns:
            ProjectionSummary with event stats, costs, timing, and node counts.

        Single-pass: every event is folded once into one accumulator.
        """
        accumulator = _SummaryAccumulator(self._budget_limit)
        for event in events:
            accumulator.process(event)
        return accumulator.build()
