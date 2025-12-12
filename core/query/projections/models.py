"""Immutable data models for the projection pipeline."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from core.domain.events import DomainEvent


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
        )
