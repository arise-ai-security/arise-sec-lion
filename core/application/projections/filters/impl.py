"""Event filtering strategy implementations.

This module provides filter implementations that determine which events
pass through the projection pipeline.

Available filters:
    - "all": IncludeAllFilter - passes all events
    - "errors_only": ErrorOnlyFilter - only WorkFailed events
    - "by_agent": AgentFilter - events from specific agent UUIDs
    - "by_type": EventTypeFilter - events of specific types
"""

from uuid import UUID

from core.application.projections.registry import register_filter
from core.domain.events import DomainEvent, WorkFailed


@register_filter("all")
class IncludeAllFilter:
    """Filter that includes all events."""

    def matches(self, event: DomainEvent) -> bool:
        """Always returns True."""
        return True


@register_filter("errors_only")
class ErrorOnlyFilter:
    """Filter that only includes error events."""

    def matches(self, event: DomainEvent) -> bool:
        """Check if the event is an error event."""
        return isinstance(event, WorkFailed)


@register_filter("by_agent")
class AgentFilter:
    """Filter that only includes events from specific agents."""

    def __init__(self, agent_ids: set[UUID]) -> None:
        """Initialize with a set of agent IDs to include."""
        self._agent_ids = frozenset(agent_ids)

    @property
    def agent_ids(self) -> frozenset[UUID]:
        """Get the set of included agent IDs."""
        return self._agent_ids

    def matches(self, event: DomainEvent) -> bool:
        """Check if the event is from an included agent."""
        return event.aggregate_id in self._agent_ids


@register_filter("by_type")
class EventTypeFilter:
    """Filter that only includes events of specific types."""

    def __init__(self, event_types: set[str]) -> None:
        """Initialize with a set of event type names."""
        self._event_types = frozenset(event_types)

    @property
    def event_types(self) -> frozenset[str]:
        """Get the set of included event types."""
        return self._event_types

    def matches(self, event: DomainEvent) -> bool:
        """Check if the event type is included."""
        return type(event).__name__ in self._event_types


class CompositeFilter:
    """Filter that combines multiple filters with AND logic."""

    def __init__(self, filters: list) -> None:
        """Initialize with a list of filters to combine."""
        self._filters = list(filters)

    def matches(self, event: DomainEvent) -> bool:
        """Check if all sub-filters match."""
        return all(f.matches(event) for f in self._filters)


class AnyOfFilter:
    """Filter that combines multiple filters with OR logic."""

    def __init__(self, filters: list) -> None:
        """Initialize with a list of filters to combine."""
        self._filters = list(filters)

    def matches(self, event: DomainEvent) -> bool:
        """Check if any sub-filter matches."""
        return any(f.matches(event) for f in self._filters)
