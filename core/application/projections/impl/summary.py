"""Summary Projection for aggregated statistics.

This module provides a projection that aggregates events into summary
statistics. Useful for dashboards, monitoring, and high-level reports.
"""

from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from core.application.projections.base import Projection
from core.application.projections.models import ProjectionSummary
from core.application.projections.registry import register_projection
from core.domain.events import DomainEvent, WorkFailed


@register_projection("summary")
class SummaryProjection(Projection):
    """Aggregates events into summary statistics.

    This projection processes events and produces a ProjectionSummary
    containing counts, statistics, and extracted error information.

    Statistics provided:
        - Total event count
        - Events grouped by type name
        - Unique agents involved
        - First/last event timestamps
        - Count and list of WorkFailed events
    """

    def project(self, events: Iterable[DomainEvent]) -> ProjectionSummary:
        """Aggregate events into summary statistics.

        Args:
            events: Iterable of domain events to aggregate.

        Returns:
            ProjectionSummary with counts and statistics.
        """
        # Materialize events since we need to iterate multiple times
        events_list = list(events)

        if not events_list:
            return ProjectionSummary.empty()

        errors = self._extract_errors(events_list)

        return ProjectionSummary(
            total_events=len(events_list),
            events_by_type=self._count_by_type(events_list),
            agents_involved=self._collect_agents(events_list),
            first_event=events_list[0].occurred_at,
            last_event=events_list[-1].occurred_at,
            error_count=len(errors),
            errors=errors,
        )

    def _count_by_type(self, events: list[DomainEvent]) -> dict[str, int]:
        """Count events grouped by type name."""
        counter: Counter[str] = Counter()
        for event in events:
            counter[type(event).__name__] += 1
        return dict(counter)

    def _collect_agents(self, events: list[DomainEvent]) -> frozenset[UUID]:
        """Collect unique agent IDs from events."""
        return frozenset(event.aggregate_id for event in events)

    def _extract_errors(self, events: list[DomainEvent]) -> tuple[DomainEvent, ...]:
        """Extract WorkFailed events."""
        return tuple(event for event in events if isinstance(event, WorkFailed))
