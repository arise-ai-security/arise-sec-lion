"""Event filter implementations."""

from uuid import UUID

from core.application.projections.registry import register_filter
from core.domain.events import DomainEvent, WorkFailed


@register_filter("all")
class IncludeAllFilter:
    """Pass all events."""

    def matches(self, event: DomainEvent) -> bool:
        return True


@register_filter("errors_only")
class ErrorOnlyFilter:
    """Only WorkFailed events."""

    def matches(self, event: DomainEvent) -> bool:
        return isinstance(event, WorkFailed)


@register_filter("by_agent")
class AgentFilter:
    """Events from specific agent UUIDs."""

    def __init__(self, agent_ids: set[UUID]) -> None:
        self._agent_ids = frozenset(agent_ids)

    @property
    def agent_ids(self) -> frozenset[UUID]:
        return self._agent_ids

    def matches(self, event: DomainEvent) -> bool:
        return event.aggregate_id in self._agent_ids


@register_filter("by_type")
class EventTypeFilter:
    """Events of specific type names."""

    def __init__(self, event_types: set[str]) -> None:
        self._event_types = frozenset(event_types)

    @property
    def event_types(self) -> frozenset[str]:
        return self._event_types

    def matches(self, event: DomainEvent) -> bool:
        return type(event).__name__ in self._event_types


class CompositeFilter:
    """AND logic: all sub-filters must match."""

    def __init__(self, filters: list) -> None:
        self._filters = list(filters)

    def matches(self, event: DomainEvent) -> bool:
        return all(f.matches(event) for f in self._filters)


class AnyOfFilter:
    """OR logic: any sub-filter matches."""

    def __init__(self, filters: list) -> None:
        self._filters = list(filters)

    def matches(self, event: DomainEvent) -> bool:
        return any(f.matches(event) for f in self._filters)
