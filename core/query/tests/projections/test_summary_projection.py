"""Tests for SummaryProjection."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from core.domain.events.events import (
    AgentCreated,
    StatusChanged,
    TaskAssigned,
    TokensConsumed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from core.domain.exceptions import CostInvariantViolation
from core.query.projections.impl import SummaryProjection
from core.query.projections.models import CostSummary, ProjectionSummary
from core.query.projections.registry import ProjectionRegistry
from core.query.tests.projections.conftest import (
    BASE_TIME,
    BOSS_ID,
    MANAGER_ID,
    WORKER2_ID,
    WORKER_ID,
)


class TestSummaryProjection:
    """Tests for SummaryProjection."""

    def test_registered_as_summary(self) -> None:
        """Should be registered as 'summary'."""
        proj_cls = ProjectionRegistry.get_projection("summary")
        assert proj_cls is SummaryProjection

    def test_project_empty_returns_empty_summary(self) -> None:
        """Should return empty summary for empty input."""
        projection = SummaryProjection()
        result = projection.project([])

        assert result.total_events == 0
        assert result.events_by_type == {}
        assert result.agents_involved == frozenset()
        assert result.first_event is None
        assert result.last_event is None
        assert result.error_count == 0
        assert result.errors == ()

    def test_project_counts_total_events(self, sample_events) -> None:
        """Should count total events."""
        projection = SummaryProjection()
        result = projection.project(sample_events)

        assert result.total_events == len(sample_events)

    def test_project_groups_by_type(self) -> None:
        """Should group events by type name."""
        agent_id = uuid4()
        events = [
            AgentCreated(aggregate_id=agent_id, sequence_number=1, role="BOSS"),
            AgentCreated(aggregate_id=agent_id, sequence_number=2, role="WORKER"),
            TaskAssigned(
                aggregate_id=agent_id,
                sequence_number=3,
                task_description="Test",
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.events_by_type["AgentCreated"] == 2
        assert result.events_by_type["TaskAssigned"] == 1

    def test_project_collects_agent_ids(self) -> None:
        """Should collect unique agent IDs."""
        id1 = uuid4()
        id2 = uuid4()
        events = [
            AgentCreated(aggregate_id=id1, sequence_number=1, role="BOSS"),
            AgentCreated(aggregate_id=id2, sequence_number=1, role="WORKER"),
            TaskAssigned(aggregate_id=id1, sequence_number=2, task_description="Test"),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.agents_involved == frozenset({id1, id2})

    def test_project_captures_first_last_event(self) -> None:
        """Should capture first and last event timestamps."""
        agent_id = uuid4()
        first_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
        last_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

        events = [
            AgentCreated(
                aggregate_id=agent_id,
                sequence_number=1,
                role="BOSS",
                occurred_at=first_time,
            ),
            WorkCompleted(
                aggregate_id=agent_id,
                sequence_number=2,
                result="Done",
                occurred_at=last_time,
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.first_event == first_time
        assert result.last_event == last_time

    def test_project_extracts_errors(self) -> None:
        """Should extract WorkFailed events as errors."""
        agent_id = uuid4()
        events = [
            AgentCreated(aggregate_id=agent_id, sequence_number=1, role="BOSS"),
            WorkFailed(aggregate_id=agent_id, sequence_number=2, reason="Error 1"),
            WorkFailed(aggregate_id=agent_id, sequence_number=3, reason="Error 2"),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.error_count == 2
        assert len(result.errors) == 2
        assert all(isinstance(e, WorkFailed) for e in result.errors)

    def test_project_returns_immutable_summary(self) -> None:
        """Should return immutable ProjectionSummary."""
        projection = SummaryProjection()
        result = projection.project([])

        assert isinstance(result, ProjectionSummary)
        with pytest.raises(AttributeError):
            result.total_events = 100  # type: ignore


class TestSummaryProjectionCosts:
    """Tests for cost calculation in SummaryProjection."""

    def test_calculates_llm_costs(self) -> None:
        """Should calculate LLM costs from TokensConsumed events."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            TokensConsumed(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                model="gpt-4o",
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost_usd=0.05,
                operation="complexity_evaluation",
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.cost is not None
        assert result.cost.llm_cost_usd == 0.05
        assert result.cost.worker_cost_usd == 0.0
        assert result.cost.total_cost_usd == 0.05
        assert result.cost.total_tokens == 1500
        assert result.cost.prompt_tokens == 1000
        assert result.cost.completion_tokens == 500

    def test_calculates_worker_costs(self) -> None:
        """Should calculate worker costs from WorkerCostRecorded events."""
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
                tool_name="claude_code",
                model="claude-3-5-sonnet",
                tokens=5000,
                cost_usd=0.50,
                duration_seconds=120.0,
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.cost is not None
        assert result.cost.llm_cost_usd == 0.0
        assert result.cost.worker_cost_usd == 0.50
        assert result.cost.total_cost_usd == 0.50

    def test_aggregates_costs_by_role(self, cost_events_hierarchy) -> None:
        """Should aggregate costs by agent role."""
        projection = SummaryProjection()
        result = projection.project(cost_events_hierarchy)

        assert result.cost is not None
        cost_by_role = result.cost.cost_by_role

        # BOSS: 0.10 (task decomposition)
        assert abs(cost_by_role.get("BOSS", 0) - 0.10) < 1e-6
        # MANAGER: 0.05 (task decomposition)
        assert abs(cost_by_role.get("MANAGER", 0) - 0.05) < 1e-6
        # WORKER: 0.01 + 0.50 + 0.30 = 0.81 (complexity + claude_code + openhands)
        assert abs(cost_by_role.get("WORKER", 0) - 0.81) < 1e-6

    def test_aggregates_costs_by_model(self, cost_events_hierarchy) -> None:
        """Should aggregate costs by model."""
        projection = SummaryProjection()
        result = projection.project(cost_events_hierarchy)

        assert result.cost is not None
        cost_by_model = result.cost.cost_by_model

        # gpt-4o: 0.10 (BOSS)
        assert abs(cost_by_model.get("gpt-4o", 0) - 0.10) < 1e-6
        # gpt-4o-mini: 0.05 + 0.01 = 0.06 (MANAGER + WORKER1)
        assert abs(cost_by_model.get("gpt-4o-mini", 0) - 0.06) < 1e-6
        # claude-3-5-sonnet-20241022: 0.50 (WORKER1 tool)
        assert abs(cost_by_model.get("claude-3-5-sonnet-20241022", 0) - 0.50) < 1e-6
        # openhands worker has no model, so it shouldn't appear

    def test_aggregates_costs_by_operation(self, cost_events_hierarchy) -> None:
        """Should aggregate costs by operation type."""
        projection = SummaryProjection()
        result = projection.project(cost_events_hierarchy)

        assert result.cost is not None
        cost_by_op = result.cost.cost_by_operation

        # task_decomposition: 0.10 + 0.05 = 0.15
        assert abs(cost_by_op.get("task_decomposition", 0) - 0.15) < 1e-6
        # complexity_evaluation: 0.01
        assert abs(cost_by_op.get("complexity_evaluation", 0) - 0.01) < 1e-6
        # worker:claude_code: 0.50
        assert abs(cost_by_op.get("worker:claude_code", 0) - 0.50) < 1e-6
        # worker:openhands: 0.30
        assert abs(cost_by_op.get("worker:openhands", 0) - 0.30) < 1e-6

    def test_aggregates_costs_by_agent(self, cost_events_hierarchy) -> None:
        """Should aggregate costs by agent ID."""
        projection = SummaryProjection()
        result = projection.project(cost_events_hierarchy)

        assert result.cost is not None
        cost_by_agent = result.cost.cost_by_agent

        # BOSS: 0.10
        assert abs(cost_by_agent.get(str(BOSS_ID), 0) - 0.10) < 1e-6
        # MANAGER: 0.05
        assert abs(cost_by_agent.get(str(MANAGER_ID), 0) - 0.05) < 1e-6
        # WORKER: 0.01 + 0.50 = 0.51
        assert abs(cost_by_agent.get(str(WORKER_ID), 0) - 0.51) < 1e-6
        # WORKER2: 0.30
        assert abs(cost_by_agent.get(str(WORKER2_ID), 0) - 0.30) < 1e-6

    def test_budget_tracking_without_limit(self) -> None:
        """Should leave budget fields None when no limit set."""
        events = [
            AgentCreated(aggregate_id=BOSS_ID, sequence_number=1, role="BOSS"),
        ]

        projection = SummaryProjection()  # No budget limit
        result = projection.project(events)

        assert result.cost is not None
        assert result.cost.budget_limit_usd is None
        assert result.cost.budget_remaining_usd is None
        assert result.cost.budget_exceeded is False

    def test_budget_tracking_with_limit(self) -> None:
        """Should calculate budget remaining when limit is set."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            TokensConsumed(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                model="gpt-4o",
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost_usd=0.30,
                operation="complexity_evaluation",
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
        ]

        projection = SummaryProjection(budget_limit_usd=1.0)
        result = projection.project(events)

        assert result.cost is not None
        assert result.cost.budget_limit_usd == 1.0
        assert abs(result.cost.budget_remaining_usd - 0.70) < 1e-6
        assert result.cost.budget_exceeded is False

    def test_budget_exceeded_detection(self) -> None:
        """Should detect when budget is exceeded."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            TokensConsumed(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                model="gpt-4o",
                prompt_tokens=1000,
                completion_tokens=500,
                total_tokens=1500,
                cost_usd=1.50,
                operation="complexity_evaluation",
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
        ]

        projection = SummaryProjection(budget_limit_usd=1.0)
        result = projection.project(events)

        assert result.cost is not None
        assert result.cost.budget_exceeded is True
        assert result.cost.budget_remaining_usd == 0.0

    def test_empty_events_has_zero_costs(self) -> None:
        """Should return None cost for empty events."""
        projection = SummaryProjection()
        result = projection.project([])

        # Empty projection returns empty summary which has None for cost
        assert result.cost is None


class TestSummaryProjectionNodeCounts:
    """Tests for node count calculation in SummaryProjection."""

    def test_counts_nodes_by_role(self, cost_events_hierarchy) -> None:
        """Should count nodes by role."""
        projection = SummaryProjection()
        result = projection.project(cost_events_hierarchy)

        assert result.node_counts is not None
        assert result.node_counts.total == 4
        assert result.node_counts.by_role.get("BOSS", 0) == 1
        assert result.node_counts.by_role.get("MANAGER", 0) == 1
        assert result.node_counts.by_role.get("WORKER", 0) == 2

    def test_empty_events_has_no_node_counts(self) -> None:
        """Should return None node_counts for empty events."""
        projection = SummaryProjection()
        result = projection.project([])

        assert result.node_counts is None


class TestSummaryProjectionTiming:
    """Tests for execution time calculation in SummaryProjection."""

    def test_calculates_total_execution_time(self) -> None:
        """Should calculate total execution time from first to last event."""
        start_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)
        end_time = datetime(2024, 1, 1, 10, 5, 0, tzinfo=UTC)  # 5 minutes later

        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=start_time,
            ),
            WorkCompleted(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                result="Done",
                occurred_at=end_time,
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.execution_time is not None
        assert result.execution_time.total_seconds == 300.0  # 5 minutes

    def test_calculates_per_agent_time(self) -> None:
        """Should calculate execution time per agent."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            WorkCompleted(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                result="Done",
                occurred_at=BASE_TIME + timedelta(seconds=10),
            ),
            AgentCreated(
                aggregate_id=WORKER_ID,
                sequence_number=1,
                role="WORKER",
                occurred_at=BASE_TIME + timedelta(seconds=5),
            ),
            WorkCompleted(
                aggregate_id=WORKER_ID,
                sequence_number=2,
                result="Done",
                occurred_at=BASE_TIME + timedelta(seconds=20),
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.execution_time is not None
        per_agent = result.execution_time.per_agent

        # BOSS: 10 seconds (from 0 to 10)
        assert per_agent.get(str(BOSS_ID), 0) == 10.0
        # WORKER: 15 seconds (from 5 to 20)
        assert per_agent.get(str(WORKER_ID), 0) == 15.0

    def test_calculates_per_role_time(self) -> None:
        """Should aggregate execution time by role."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            WorkCompleted(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                result="Done",
                occurred_at=BASE_TIME + timedelta(seconds=10),
            ),
            AgentCreated(
                aggregate_id=WORKER_ID,
                sequence_number=1,
                role="WORKER",
                occurred_at=BASE_TIME + timedelta(seconds=5),
            ),
            WorkCompleted(
                aggregate_id=WORKER_ID,
                sequence_number=2,
                result="Done",
                occurred_at=BASE_TIME + timedelta(seconds=20),
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.execution_time is not None
        per_role = result.execution_time.per_role

        assert per_role.get("BOSS", 0) == 10.0
        assert per_role.get("WORKER", 0) == 15.0

    def test_calculates_phase_times(self) -> None:
        """Should calculate time spent in each phase from StatusChanged events."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            StatusChanged(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                old_status="PENDING",
                new_status="ANALYZING",
                occurred_at=BASE_TIME + timedelta(seconds=5),
            ),
            StatusChanged(
                aggregate_id=BOSS_ID,
                sequence_number=3,
                old_status="ANALYZING",
                new_status="IN_PROGRESS",
                occurred_at=BASE_TIME + timedelta(seconds=15),
            ),
            StatusChanged(
                aggregate_id=BOSS_ID,
                sequence_number=4,
                old_status="IN_PROGRESS",
                new_status="COMPLETED",
                occurred_at=BASE_TIME + timedelta(seconds=30),
            ),
        ]

        projection = SummaryProjection()
        result = projection.project(events)

        assert result.execution_time is not None
        per_phase = result.execution_time.per_phase

        # ANALYZING: 10 seconds (from 5 to 15)
        assert per_phase.get("analyzing", 0) == 10.0
        # IN_PROGRESS: 15 seconds (from 15 to 30)
        assert per_phase.get("in_progress", 0) == 15.0


class TestCostSummaryInvariants:
    """Tests for CostSummary invariant validation."""

    def test_valid_cost_summary_passes_validation(self) -> None:
        """Should not raise for valid cost summary."""
        # This should not raise
        summary = CostSummary(
            total_cost_usd=0.55,
            llm_cost_usd=0.05,
            worker_cost_usd=0.50,
            total_tokens=1500,
            prompt_tokens=1000,
            completion_tokens=500,
            cost_by_model={"gpt-4o": 0.05, "claude-3": 0.50},
            cost_by_operation={"eval": 0.05, "worker:code": 0.50},
            cost_by_agent={"agent-1": 0.05, "agent-2": 0.50},
            cost_by_role={"BOSS": 0.05, "WORKER": 0.50},
            tokens_by_role={"BOSS": 500, "WORKER": 1000},
        )
        assert summary.total_cost_usd == 0.55

    def test_total_cost_mismatch_raises(self) -> None:
        """Should raise when total != llm + worker."""
        with pytest.raises(CostInvariantViolation) as exc_info:
            CostSummary(
                total_cost_usd=1.0,  # Wrong: should be 0.55
                llm_cost_usd=0.05,
                worker_cost_usd=0.50,
                total_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
            )
        assert "total_cost == llm_cost + worker_cost" in str(exc_info.value)

    def test_cost_by_operation_sum_mismatch_raises(self) -> None:
        """Should raise when sum(cost_by_operation) != total."""
        with pytest.raises(CostInvariantViolation) as exc_info:
            CostSummary(
                total_cost_usd=0.55,
                llm_cost_usd=0.05,
                worker_cost_usd=0.50,
                total_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
                cost_by_operation={"eval": 0.10},  # Wrong: should sum to 0.55
            )
        assert "sum(cost_by_operation) == total_cost" in str(exc_info.value)

    def test_negative_cost_raises(self) -> None:
        """Should raise for negative costs."""
        with pytest.raises(CostInvariantViolation) as exc_info:
            CostSummary(
                total_cost_usd=-0.10,
                llm_cost_usd=-0.10,
                worker_cost_usd=0.0,
                total_tokens=0,
                prompt_tokens=0,
                completion_tokens=0,
            )
        assert "all_costs >= 0" in str(exc_info.value)

    def test_empty_cost_summary_factory(self) -> None:
        """Should create valid empty cost summary."""
        summary = CostSummary.empty()
        assert summary.total_cost_usd == 0.0
        assert summary.llm_cost_usd == 0.0
        assert summary.worker_cost_usd == 0.0

    def test_empty_cost_summary_with_budget(self) -> None:
        """Should create valid empty cost summary with budget."""
        summary = CostSummary.empty(budget_limit=10.0)
        assert summary.budget_limit_usd == 10.0
        assert summary.budget_remaining_usd == 10.0
        assert summary.budget_exceeded is False
