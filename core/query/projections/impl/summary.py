"""Summary projection: aggregate events into statistics."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from core.domain.events.events import (
    AgentCreated,
    DomainEvent,
    StatusChanged,
    TokensConsumed,
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
        """
        events_list = list(events)
        if not events_list:
            return ProjectionSummary.empty()

        # Build role map from AgentCreated events
        role_map = self._build_role_map(events_list)

        # Calculate all metrics
        errors = self._extract_errors(events_list)
        cost = self._calculate_costs(events_list, role_map)
        timing = self._calculate_execution_times(events_list, role_map)
        node_counts = self._calculate_node_counts(role_map)

        return ProjectionSummary(
            total_events=len(events_list),
            events_by_type=self._count_by_type(events_list),
            agents_involved=frozenset(role_map.keys()),
            first_event=events_list[0].occurred_at,
            last_event=events_list[-1].occurred_at,
            error_count=len(errors),
            errors=errors,
            node_counts=node_counts,
            cost=cost,
            execution_time=timing,
        )

    def _build_role_map(self, events: list[DomainEvent]) -> dict[UUID, str]:
        """Build agent_id -> role mapping from AgentCreated events.

        Args:
            events: List of domain events.

        Returns:
            Mapping of agent UUID to role string (uppercase).
        """
        role_map: dict[UUID, str] = {}

        for event in events:
            if isinstance(event, AgentCreated):
                role_map[event.aggregate_id] = event.role.upper()

        return role_map

    def _calculate_costs(
        self, events: list[DomainEvent], role_map: dict[UUID, str]
    ) -> CostSummary:
        """Calculate cost breakdown with role correlation.

        Args:
            events: List of domain events.
            role_map: Mapping of agent UUID to role string.

        Returns:
            CostSummary with all breakdowns.
        """
        llm_cost = 0.0
        worker_cost = 0.0
        total_tokens = 0
        prompt_tokens = 0
        completion_tokens = 0

        cost_by_model: dict[str, float] = defaultdict(float)
        cost_by_operation: dict[str, float] = defaultdict(float)
        cost_by_agent: dict[str, float] = defaultdict(float)
        cost_by_role: dict[str, float] = defaultdict(float)
        tokens_by_role: dict[str, int] = defaultdict(int)

        budget_exceeded = False

        for event in events:
            if isinstance(event, TokensConsumed):
                agent_id = event.aggregate_id
                role = role_map.get(agent_id, "UNKNOWN")

                llm_cost += event.cost_usd
                total_tokens += event.total_tokens
                prompt_tokens += event.prompt_tokens
                completion_tokens += event.completion_tokens

                cost_by_model[event.model] += event.cost_usd
                cost_by_operation[event.operation] += event.cost_usd
                cost_by_agent[str(agent_id)] += event.cost_usd
                cost_by_role[role] += event.cost_usd
                tokens_by_role[role] += event.total_tokens

            elif isinstance(event, WorkerCostRecorded):
                agent_id = event.aggregate_id
                role = role_map.get(agent_id, "UNKNOWN")

                worker_cost += event.cost_usd
                if event.model:
                    cost_by_model[event.model] += event.cost_usd

                operation_key = f"worker:{event.tool_name}"
                cost_by_operation[operation_key] += event.cost_usd
                cost_by_agent[str(agent_id)] += event.cost_usd
                cost_by_role[role] += event.cost_usd

                if event.tokens:
                    total_tokens += event.tokens
                    tokens_by_role[role] += event.tokens

        total_cost = llm_cost + worker_cost

        # Budget calculation
        budget_remaining = None
        if self._budget_limit is not None:
            budget_remaining = max(0.0, self._budget_limit - total_cost)
            if total_cost >= self._budget_limit:
                budget_exceeded = True

        # Round all costs to 6 decimal places for consistency
        return CostSummary(
            total_cost_usd=round(total_cost, 6),
            llm_cost_usd=round(llm_cost, 6),
            worker_cost_usd=round(worker_cost, 6),
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_by_model={k: round(v, 6) for k, v in cost_by_model.items()},
            cost_by_operation={k: round(v, 6) for k, v in cost_by_operation.items()},
            cost_by_agent={k: round(v, 6) for k, v in cost_by_agent.items()},
            cost_by_role={k: round(v, 6) for k, v in cost_by_role.items()},
            tokens_by_role=dict(tokens_by_role),
            budget_limit_usd=self._budget_limit,
            budget_remaining_usd=(
                round(budget_remaining, 6) if budget_remaining is not None else None
            ),
            budget_exceeded=budget_exceeded,
        )

    def _calculate_execution_times(
        self, events: list[DomainEvent], role_map: dict[UUID, str]
    ) -> ExecutionTimeSummary:
        """Calculate execution time breakdown from event timestamps.

        Args:
            events: List of domain events.
            role_map: Mapping of agent UUID to role string.

        Returns:
            ExecutionTimeSummary with time breakdowns.
        """
        if not events:
            return ExecutionTimeSummary.empty()

        # Track first/last event per agent
        agent_first: dict[UUID, datetime] = {}
        agent_last: dict[UUID, datetime] = {}

        # Track phase times via StatusChanged events
        phase_times: dict[str, float] = defaultdict(float)
        agent_phase_start: dict[tuple[UUID, str], datetime] = {}

        for event in events:
            agent_id = event.aggregate_id

            # Track first/last event per agent
            if agent_id not in agent_first:
                agent_first[agent_id] = event.occurred_at
            agent_last[agent_id] = event.occurred_at

            # Track phase transitions for duration calculation
            if isinstance(event, StatusChanged):
                old_phase = event.old_status.lower()
                new_phase = event.new_status.lower()
                phase_key = (agent_id, old_phase)

                # End the old phase if we were tracking it
                if phase_key in agent_phase_start:
                    start_time = agent_phase_start[phase_key]
                    duration = (event.occurred_at - start_time).total_seconds()
                    phase_times[old_phase] += duration
                    del agent_phase_start[phase_key]

                # Start tracking the new phase
                agent_phase_start[(agent_id, new_phase)] = event.occurred_at

        # Calculate per-agent execution times
        per_agent: dict[str, float] = {}
        for agent_id in agent_first:
            first = agent_first[agent_id]
            last = agent_last[agent_id]
            per_agent[str(agent_id)] = (last - first).total_seconds()

        # Calculate per-role execution times
        per_role: dict[str, float] = defaultdict(float)
        for agent_id_str, duration in per_agent.items():
            uuid_id = UUID(agent_id_str)
            role = role_map.get(uuid_id, "UNKNOWN")
            per_role[role] += duration

        # Total time is wall-clock from first to last event
        total_seconds = (events[-1].occurred_at - events[0].occurred_at).total_seconds()

        return ExecutionTimeSummary(
            total_seconds=total_seconds,
            per_role=dict(per_role),
            per_phase=dict(phase_times),
            per_agent=per_agent,
        )

    def _calculate_node_counts(self, role_map: dict[UUID, str]) -> NodeCountSummary:
        """Calculate node counts by role.

        Args:
            role_map: Mapping of agent UUID to role string.

        Returns:
            NodeCountSummary with total and by-role counts.
        """
        by_role: dict[str, int] = defaultdict(int)
        for role in role_map.values():
            by_role[role] += 1

        return NodeCountSummary(
            total=len(role_map),
            by_role=dict(by_role),
        )

    def _count_by_type(self, events: list[DomainEvent]) -> dict[str, int]:
        """Count events by type name.

        Args:
            events: List of domain events.

        Returns:
            Dict mapping event type name to count.
        """
        counter: Counter[str] = Counter()
        for event in events:
            counter[type(event).__name__] += 1
        return dict(counter)

    def _collect_agents(self, events: list[DomainEvent]) -> frozenset[UUID]:
        """Collect unique agent IDs from events.

        Args:
            events: List of domain events.

        Returns:
            Frozenset of unique agent UUIDs.
        """
        return frozenset(event.aggregate_id for event in events)

    def _extract_errors(self, events: list[DomainEvent]) -> tuple[DomainEvent, ...]:
        """Extract WorkFailed events as errors.

        Args:
            events: List of domain events.

        Returns:
            Tuple of WorkFailed events.
        """
        return tuple(event for event in events if isinstance(event, WorkFailed))
