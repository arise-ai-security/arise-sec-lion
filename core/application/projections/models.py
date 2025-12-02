"""Data models for the Event Projection System.

This module defines immutable data structures used throughout the projection
pipeline. All models are frozen dataclasses to ensure immutability.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from core.domain.events import DomainEvent


@dataclass(frozen=True)
class ProjectionSummary:
    """Aggregated statistics from a projection run.

    Provides a high-level overview of the events processed, useful for
    dashboards, reports, and monitoring.

    Attributes:
        total_events: Total number of events processed.
        events_by_type: Count of events grouped by event type name.
        agents_involved: Set of unique agent UUIDs that produced events.
        first_event: Timestamp of the earliest event (None if no events).
        last_event: Timestamp of the latest event (None if no events).
        error_count: Number of WorkFailed events.
        errors: List of WorkFailed events for inspection.
    """

    total_events: int
    events_by_type: dict[str, int]
    agents_involved: frozenset[UUID]
    first_event: datetime | None
    last_event: datetime | None
    error_count: int
    errors: tuple[DomainEvent, ...]

    @classmethod
    def empty(cls) -> "ProjectionSummary":
        """Create an empty summary with zero counts.

        Returns:
            A ProjectionSummary with all counts at zero and empty collections.
        """
        return cls(
            total_events=0,
            events_by_type={},
            agents_involved=frozenset(),
            first_event=None,
            last_event=None,
            error_count=0,
            errors=(),
        )
