"""Property-based tests for SummaryProjection using Hypothesis."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from hypothesis import given, settings, strategies as st

from core.domain.events import (
    AgentCreated,
    BudgetExceeded,
    DomainEvent,
    TokensConsumed,
    WorkerCostRecorded,
)
from core.query.projections.impl import SummaryProjection


# =============================================================================
# Hypothesis Strategies
# =============================================================================

# Strategy for valid model names
model_names = st.sampled_from([
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
])

# Strategy for operation types
llm_operations = st.sampled_from([
    "complexity_evaluation",
    "task_decomposition",
    "worker_execution",
])

# Strategy for worker tool names
tool_names = st.sampled_from(["claude_code", "openhands"])

# Strategy for agent roles
agent_roles = st.sampled_from(["BOSS", "MANAGER", "WORKER", "PENDING"])

# Strategy for non-negative integers (token counts)
token_count = st.integers(min_value=0, max_value=1_000_000)

# Strategy for non-negative costs (USD) - reasonable range
cost_usd = st.floats(
    min_value=0.0,
    max_value=10.0,
    allow_nan=False,
    allow_infinity=False,
).map(lambda x: round(x, 6))

# Strategy for duration in seconds
duration_seconds = st.floats(
    min_value=0.0,
    max_value=3600.0,
    allow_nan=False,
    allow_infinity=False,
)

# Base time for events
BASE_TIME = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


@st.composite
def agent_created_event(draw, agent_id=None, role=None, seq_num=None, time_offset=None):
    """Generate a valid AgentCreated event."""
    return AgentCreated(
        aggregate_id=agent_id or draw(st.uuids()),
        sequence_number=seq_num or draw(st.integers(min_value=1, max_value=1000)),
        role=role or draw(agent_roles),
        occurred_at=BASE_TIME + timedelta(seconds=time_offset or draw(st.integers(0, 1000))),
    )


@st.composite
def tokens_consumed_event(draw, agent_id=None, seq_num=None, time_offset=None):
    """Generate a valid TokensConsumed event."""
    prompt = draw(token_count)
    completion = draw(token_count)
    total = prompt + completion

    return TokensConsumed(
        aggregate_id=agent_id or draw(st.uuids()),
        sequence_number=seq_num or draw(st.integers(min_value=1, max_value=1000)),
        model=draw(model_names),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=draw(cost_usd),
        operation=draw(llm_operations),
        occurred_at=BASE_TIME + timedelta(seconds=time_offset or draw(st.integers(0, 1000))),
    )


@st.composite
def worker_cost_event(draw, agent_id=None, seq_num=None, time_offset=None):
    """Generate a valid WorkerCostRecorded event."""
    include_model = draw(st.booleans())
    include_tokens = draw(st.booleans())

    return WorkerCostRecorded(
        aggregate_id=agent_id or draw(st.uuids()),
        sequence_number=seq_num or draw(st.integers(min_value=1, max_value=1000)),
        tool_name=draw(tool_names),
        model=draw(model_names) if include_model else None,
        tokens=draw(token_count) if include_tokens else None,
        cost_usd=draw(cost_usd),
        duration_seconds=draw(duration_seconds),
        occurred_at=BASE_TIME + timedelta(seconds=time_offset or draw(st.integers(0, 1000))),
    )


@st.composite
def cost_event_sequence(draw):
    """Generate a sequence of cost-related events with proper agent lifecycle.

    Each agent gets an AgentCreated event before any cost events.
    """
    # Generate a pool of agent IDs
    num_agents = draw(st.integers(min_value=1, max_value=5))
    agent_pool = [uuid4() for _ in range(num_agents)]
    agent_roles_map = {
        agent_id: draw(agent_roles) for agent_id in agent_pool
    }

    events: list[DomainEvent] = []
    time_offset = 0

    # First, create all agents
    for agent_id in agent_pool:
        events.append(
            AgentCreated(
                aggregate_id=agent_id,
                sequence_number=1,
                role=agent_roles_map[agent_id],
                occurred_at=BASE_TIME + timedelta(seconds=time_offset),
            )
        )
        time_offset += 1

    # Then add cost events
    num_cost_events = draw(st.integers(min_value=0, max_value=20))
    for _ in range(num_cost_events):
        agent_id = draw(st.sampled_from(agent_pool))
        event_type = draw(st.sampled_from(["tokens", "worker"]))

        if event_type == "tokens":
            events.append(draw(tokens_consumed_event(
                agent_id=agent_id,
                seq_num=len([e for e in events if e.aggregate_id == agent_id]) + 1,
                time_offset=time_offset,
            )))
        else:
            events.append(draw(worker_cost_event(
                agent_id=agent_id,
                seq_num=len([e for e in events if e.aggregate_id == agent_id]) + 1,
                time_offset=time_offset,
            )))
        time_offset += 1

    return events


# =============================================================================
# Property Tests
# =============================================================================

@pytest.mark.property
class TestSummaryProjectionCostProperties:
    """Property-based tests for cost calculation invariants."""

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_total_cost_equals_sum_of_parts(self, events: list[DomainEvent]) -> None:
        """Property: total_cost_usd == llm_cost_usd + worker_cost_usd."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.cost is None:
            return  # Empty events case

        expected_total = summary.cost.llm_cost_usd + summary.cost.worker_cost_usd
        assert abs(summary.cost.total_cost_usd - expected_total) < 1e-6, (
            f"Total cost {summary.cost.total_cost_usd} != "
            f"LLM {summary.cost.llm_cost_usd} + Worker {summary.cost.worker_cost_usd}"
        )

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_sum_by_operation_equals_total(self, events: list[DomainEvent]) -> None:
        """Property: sum(cost_by_operation.values()) == total_cost_usd."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.cost is None:
            return  # Empty events case

        sum_by_operation = sum(summary.cost.cost_by_operation.values())
        assert abs(sum_by_operation - summary.cost.total_cost_usd) < 1e-6, (
            f"Sum by operation {sum_by_operation} != total {summary.cost.total_cost_usd}"
        )

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_sum_by_agent_equals_total(self, events: list[DomainEvent]) -> None:
        """Property: sum(cost_by_agent.values()) == total_cost_usd."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.cost is None:
            return  # Empty events case

        sum_by_agent = sum(summary.cost.cost_by_agent.values())
        assert abs(sum_by_agent - summary.cost.total_cost_usd) < 1e-6, (
            f"Sum by agent {sum_by_agent} != total {summary.cost.total_cost_usd}"
        )

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_sum_by_role_equals_total(self, events: list[DomainEvent]) -> None:
        """Property: sum(cost_by_role.values()) == total_cost_usd."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.cost is None:
            return  # Empty events case

        sum_by_role = sum(summary.cost.cost_by_role.values())
        assert abs(sum_by_role - summary.cost.total_cost_usd) < 1e-6, (
            f"Sum by role {sum_by_role} != total {summary.cost.total_cost_usd}"
        )

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_costs_are_non_negative(self, events: list[DomainEvent]) -> None:
        """Property: all costs must be non-negative."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.cost is None:
            return  # Empty events case

        assert summary.cost.total_cost_usd >= 0
        assert summary.cost.llm_cost_usd >= 0
        assert summary.cost.worker_cost_usd >= 0
        assert all(v >= 0 for v in summary.cost.cost_by_model.values())
        assert all(v >= 0 for v in summary.cost.cost_by_operation.values())
        assert all(v >= 0 for v in summary.cost.cost_by_agent.values())
        assert all(v >= 0 for v in summary.cost.cost_by_role.values())

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_token_counts_are_non_negative(self, events: list[DomainEvent]) -> None:
        """Property: all token counts must be non-negative."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.cost is None:
            return  # Empty events case

        assert summary.cost.total_tokens >= 0
        assert summary.cost.prompt_tokens >= 0
        assert summary.cost.completion_tokens >= 0


@pytest.mark.property
class TestSummaryProjectionNodeCountProperties:
    """Property-based tests for node count invariants."""

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_node_count_by_role_sums_to_total(self, events: list[DomainEvent]) -> None:
        """Property: sum(by_role.values()) == total nodes."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.node_counts is None:
            return  # Empty events case

        role_sum = sum(summary.node_counts.by_role.values())
        assert role_sum == summary.node_counts.total, (
            f"Sum by role {role_sum} != total {summary.node_counts.total}"
        )

    @given(events=cost_event_sequence())
    @settings(max_examples=100, deadline=None)
    def test_agents_involved_matches_node_count(self, events: list[DomainEvent]) -> None:
        """Property: len(agents_involved) == node_counts.total."""
        projection = SummaryProjection()
        summary = projection.project(events)

        if summary.node_counts is None:
            return  # Empty events case

        assert len(summary.agents_involved) == summary.node_counts.total


@pytest.mark.property
class TestSummaryProjectionIdempotence:
    """Property-based tests for projection idempotence."""

    @given(events=cost_event_sequence())
    @settings(max_examples=50, deadline=None)
    def test_projection_is_idempotent(self, events: list[DomainEvent]) -> None:
        """Property: projecting same events twice yields identical results."""
        projection = SummaryProjection()

        summary1 = projection.project(events)
        summary2 = projection.project(events)

        assert summary1.total_events == summary2.total_events
        assert summary1.events_by_type == summary2.events_by_type
        assert summary1.agents_involved == summary2.agents_involved
        assert summary1.error_count == summary2.error_count

        if summary1.cost is not None and summary2.cost is not None:
            assert summary1.cost.total_cost_usd == summary2.cost.total_cost_usd
            assert summary1.cost.cost_by_role == summary2.cost.cost_by_role

    @given(events1=cost_event_sequence(), events2=cost_event_sequence())
    @settings(max_examples=50, deadline=None)
    def test_projection_is_additive(
        self, events1: list[DomainEvent], events2: list[DomainEvent]
    ) -> None:
        """Property: total cost of combined events == sum of individual totals."""
        # Filter to cost events only
        cost_events1 = [
            e for e in events1
            if isinstance(e, (TokensConsumed, WorkerCostRecorded, AgentCreated))
        ]
        cost_events2 = [
            e for e in events2
            if isinstance(e, (TokensConsumed, WorkerCostRecorded, AgentCreated))
        ]

        projection = SummaryProjection()

        summary1 = projection.project(cost_events1)
        summary2 = projection.project(cost_events2)
        summary_combined = projection.project(cost_events1 + cost_events2)

        # Get costs (default to 0 if None)
        cost1 = summary1.cost.total_cost_usd if summary1.cost else 0.0
        cost2 = summary2.cost.total_cost_usd if summary2.cost else 0.0
        cost_combined = summary_combined.cost.total_cost_usd if summary_combined.cost else 0.0

        # Combined should equal sum (within tolerance)
        expected_total = cost1 + cost2
        assert abs(cost_combined - expected_total) < 1e-6, (
            f"Combined cost {cost_combined} != sum {expected_total}"
        )
