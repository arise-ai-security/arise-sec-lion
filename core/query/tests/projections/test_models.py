"""Tests for projection data models."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.domain.events import WorkFailed
from core.query.projections.models import ProjectionSummary


class TestProjectionSummary:
    """Tests for ProjectionSummary dataclass."""

    def test_create_summary(self) -> None:
        """Should create a summary with all fields."""
        agent_id = uuid4()
        first = datetime.now(UTC)
        last = datetime.now(UTC)
        error_event = WorkFailed(
            aggregate_id=agent_id,
            sequence_number=1,
            reason="Test error",
            occurred_at=first,
        )

        summary = ProjectionSummary(
            total_events=10,
            events_by_type={"AgentCreated": 5, "TaskAssigned": 5},
            agents_involved=frozenset({agent_id}),
            first_event=first,
            last_event=last,
            error_count=1,
            errors=(error_event,),
        )

        assert summary.total_events == 10
        assert summary.events_by_type == {"AgentCreated": 5, "TaskAssigned": 5}
        assert summary.agents_involved == frozenset({agent_id})
        assert summary.first_event == first
        assert summary.last_event == last
        assert summary.error_count == 1
        assert len(summary.errors) == 1

    def test_summary_is_frozen(self) -> None:
        """ProjectionSummary should be immutable."""
        summary = ProjectionSummary(
            total_events=10,
            events_by_type={},
            agents_involved=frozenset(),
            first_event=None,
            last_event=None,
            error_count=0,
            errors=(),
        )

        with pytest.raises(AttributeError):
            summary.total_events = 20  # type: ignore

    def test_empty_summary(self) -> None:
        """Should create an empty summary via factory method."""
        summary = ProjectionSummary.empty()

        assert summary.total_events == 0
        assert summary.events_by_type == {}
        assert summary.agents_involved == frozenset()
        assert summary.first_event is None
        assert summary.last_event is None
        assert summary.error_count == 0
        assert summary.errors == ()
