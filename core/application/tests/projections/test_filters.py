"""Tests for event filters."""

from uuid import uuid4

from core.application.projections.filters import (
    AgentFilter,
    AnyOfFilter,
    CompositeFilter,
    ErrorOnlyFilter,
    EventTypeFilter,
    IncludeAllFilter,
)
from core.application.projections.registry import ProjectionRegistry
from core.domain.events import (
    AgentCreated,
    TaskAssigned,
    WorkFailed,
)


class TestIncludeAllFilter:
    """Tests for IncludeAllFilter."""

    def test_matches_any_event(
        self, agent_created_event, task_assigned_event, work_failed_event
    ) -> None:
        """Should match all events."""
        filter_ = IncludeAllFilter()

        assert filter_.matches(agent_created_event)
        assert filter_.matches(task_assigned_event)
        assert filter_.matches(work_failed_event)

    def test_registered_as_all(self) -> None:
        """Should be registered as 'all'."""
        filter_cls = ProjectionRegistry.get_filter("all")
        assert filter_cls is IncludeAllFilter


class TestErrorOnlyFilter:
    """Tests for ErrorOnlyFilter."""

    def test_matches_work_failed(self, work_failed_event) -> None:
        """Should match WorkFailed events."""
        filter_ = ErrorOnlyFilter()
        assert filter_.matches(work_failed_event)

    def test_does_not_match_other_events(self, agent_created_event, work_completed_event) -> None:
        """Should not match non-error events."""
        filter_ = ErrorOnlyFilter()
        assert not filter_.matches(agent_created_event)
        assert not filter_.matches(work_completed_event)

    def test_registered_as_errors_only(self) -> None:
        """Should be registered as 'errors_only'."""
        filter_cls = ProjectionRegistry.get_filter("errors_only")
        assert filter_cls is ErrorOnlyFilter


class TestAgentFilter:
    """Tests for AgentFilter."""

    def test_matches_included_agent(self, agent_created_event) -> None:
        """Should match events from included agents."""
        agent_id = agent_created_event.aggregate_id
        filter_ = AgentFilter({agent_id})

        assert filter_.matches(agent_created_event)

    def test_does_not_match_excluded_agent(self, agent_created_event) -> None:
        """Should not match events from excluded agents."""
        other_id = uuid4()
        filter_ = AgentFilter({other_id})

        assert not filter_.matches(agent_created_event)

    def test_multiple_agent_ids(self) -> None:
        """Should work with multiple agent IDs."""
        id1 = uuid4()
        id2 = uuid4()
        filter_ = AgentFilter({id1, id2})

        event1 = AgentCreated(aggregate_id=id1, sequence_number=1, role="BOSS")
        event2 = AgentCreated(aggregate_id=id2, sequence_number=1, role="WORKER")
        event3 = AgentCreated(aggregate_id=uuid4(), sequence_number=1, role="MANAGER")

        assert filter_.matches(event1)
        assert filter_.matches(event2)
        assert not filter_.matches(event3)

    def test_agent_ids_property_is_immutable(self) -> None:
        """agent_ids property should return frozenset."""
        id1 = uuid4()
        filter_ = AgentFilter({id1})

        assert isinstance(filter_.agent_ids, frozenset)

    def test_registered_as_by_agent(self) -> None:
        """Should be registered as 'by_agent'."""
        filter_cls = ProjectionRegistry.get_filter("by_agent")
        assert filter_cls is AgentFilter


class TestEventTypeFilter:
    """Tests for EventTypeFilter."""

    def test_matches_included_type(self, agent_created_event) -> None:
        """Should match events of included types."""
        filter_ = EventTypeFilter({"AgentCreated"})
        assert filter_.matches(agent_created_event)

    def test_does_not_match_excluded_type(self, agent_created_event) -> None:
        """Should not match events of excluded types."""
        filter_ = EventTypeFilter({"WorkFailed"})
        assert not filter_.matches(agent_created_event)

    def test_multiple_event_types(
        self, agent_created_event, task_assigned_event, work_failed_event
    ) -> None:
        """Should work with multiple event types."""
        filter_ = EventTypeFilter({"AgentCreated", "TaskAssigned"})

        assert filter_.matches(agent_created_event)
        assert filter_.matches(task_assigned_event)
        assert not filter_.matches(work_failed_event)

    def test_event_types_property_is_immutable(self) -> None:
        """event_types property should return frozenset."""
        filter_ = EventTypeFilter({"AgentCreated"})
        assert isinstance(filter_.event_types, frozenset)

    def test_registered_as_by_type(self) -> None:
        """Should be registered as 'by_type'."""
        filter_cls = ProjectionRegistry.get_filter("by_type")
        assert filter_cls is EventTypeFilter


class TestCompositeFilter:
    """Tests for CompositeFilter (AND logic)."""

    def test_matches_when_all_match(self) -> None:
        """Should match when all sub-filters match."""
        agent_id = uuid4()
        event = WorkFailed(
            aggregate_id=agent_id,
            sequence_number=1,
            reason="Error",
        )

        filter_ = CompositeFilter(
            [
                ErrorOnlyFilter(),
                AgentFilter({agent_id}),
            ]
        )

        assert filter_.matches(event)

    def test_does_not_match_when_any_fails(self) -> None:
        """Should not match when any sub-filter fails."""
        event = WorkFailed(
            aggregate_id=uuid4(),
            sequence_number=1,
            reason="Error",
        )

        filter_ = CompositeFilter(
            [
                ErrorOnlyFilter(),
                AgentFilter({uuid4()}),  # Different agent
            ]
        )

        assert not filter_.matches(event)

    def test_empty_composite_matches_all(self) -> None:
        """Empty composite should match all (vacuous truth)."""
        event = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=1,
            role="BOSS",
        )

        filter_ = CompositeFilter([])
        assert filter_.matches(event)


class TestAnyOfFilter:
    """Tests for AnyOfFilter (OR logic)."""

    def test_matches_when_any_matches(self) -> None:
        """Should match when any sub-filter matches."""
        event = WorkFailed(
            aggregate_id=uuid4(),
            sequence_number=1,
            reason="Error",
        )

        filter_ = AnyOfFilter(
            [
                EventTypeFilter({"AgentCreated"}),
                ErrorOnlyFilter(),
            ]
        )

        assert filter_.matches(event)

    def test_does_not_match_when_none_match(self) -> None:
        """Should not match when no sub-filters match."""
        event = TaskAssigned(
            aggregate_id=uuid4(),
            sequence_number=1,
            task_description="Test",
        )

        filter_ = AnyOfFilter(
            [
                EventTypeFilter({"AgentCreated"}),
                ErrorOnlyFilter(),
            ]
        )

        assert not filter_.matches(event)

    def test_empty_anyof_matches_none(self) -> None:
        """Empty AnyOf should match nothing."""
        event = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=1,
            role="BOSS",
        )

        filter_ = AnyOfFilter([])
        assert not filter_.matches(event)
