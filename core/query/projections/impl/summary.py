"""Summary projection: aggregate events into statistics."""

from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from core.domain.events import DomainEvent, WorkFailed
from core.query.projections.base import Projection
from core.query.projections.models import ProjectionSummary
from core.query.projections.registry import register_projection


@register_projection("summary")
class SummaryProjection(Projection):
    """Aggregate events into counts, timestamps, and error list."""

    def project(self, events: Iterable[DomainEvent]) -> ProjectionSummary:
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
        counter: Counter[str] = Counter()
        for event in events:
            counter[type(event).__name__] += 1
        return dict(counter)

    def _collect_agents(self, events: list[DomainEvent]) -> frozenset[UUID]:
        return frozenset(event.aggregate_id for event in events)

    def _extract_errors(self, events: list[DomainEvent]) -> tuple[DomainEvent, ...]:
        return tuple(event for event in events if isinstance(event, WorkFailed))
