"""Tests for SummaryProjection."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.domain.events import (
    AgentCreated,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)
from core.query.projections.impl import SummaryProjection
from core.query.projections.models import ProjectionSummary
from core.query.projections.registry import ProjectionRegistry


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
