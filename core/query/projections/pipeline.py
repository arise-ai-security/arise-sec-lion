"""Projection pipeline: collect → filter → format → sink."""

from typing import Any, Self
from uuid import UUID

from core.domain.events import DomainEvent
from core.ports.event_store_port import EventStorePort
from core.query.ports.sink_port import SinkPort
from core.query.projections.base import EventFilter, Formatter
from core.query.projections.filters import IncludeAllFilter
from core.query.projections.formatters import JSONFormatter
from core.query.projections.hierarchy_collector import HierarchyCollector
from core.query.projections.impl import SummaryProjection
from core.query.projections.models import ProjectionSummary
from core.query.projections.registry import ProjectionRegistry


class ProjectionPipeline:
    """Orchestrates: collect events → filter → format → output to sink."""

    def __init__(
        self,
        event_store: EventStorePort,
        filter_: EventFilter,
        formatter: Formatter,
        sink: SinkPort,
        output_type: str = "events",
    ) -> None:
        self._event_store = event_store
        self._filter = filter_
        self._formatter = formatter
        self._sink = sink
        self._output_type = output_type
        self._collector = HierarchyCollector(event_store)

    async def execute(self, root_agent_id: UUID) -> None:
        """Execute full pipeline: collect → filter → format → sink."""
        events = await self._collector.collect(root_agent_id)
        filtered = [e for e in events if self._filter.matches(e)]

        if self._output_type == "summary":
            projection = SummaryProjection()
            summary = projection.project(filtered)
            output = self._formatter.format_summary(summary)
        else:
            output = self._formatter.format(filtered)

        self._sink.write(output)

    async def execute_events(self, root_agent_id: UUID) -> list[DomainEvent]:
        """Return filtered events without formatting or sink output."""
        events = await self._collector.collect(root_agent_id)
        return [e for e in events if self._filter.matches(e)]

    async def execute_summary(self, root_agent_id: UUID) -> ProjectionSummary:
        """Return aggregated summary without sink output."""
        events = await self._collector.collect(root_agent_id)
        filtered = [e for e in events if self._filter.matches(e)]
        projection = SummaryProjection()
        return projection.project(filtered)


class ProjectionPipelineBuilder:
    """Fluent builder for constructing projection pipelines."""

    def __init__(self, event_store: EventStorePort) -> None:
        self._event_store = event_store
        self._filter: EventFilter = IncludeAllFilter()
        self._output_type: str = "events"
        self._formatter: Formatter = JSONFormatter()
        self._sink: SinkPort | None = None

    def with_filter(self, name: str, **kwargs: Any) -> Self:
        """Set filter by registered name."""
        filter_cls = ProjectionRegistry.get_filter(name)
        self._filter = filter_cls(**kwargs) if kwargs else filter_cls()
        return self

    def with_filter_instance(self, filter_: EventFilter) -> Self:
        """Set pre-configured filter instance."""
        self._filter = filter_
        return self

    def with_output(self, output_type: str) -> Self:
        """Set output type: 'events' or 'summary'."""
        self._output_type = output_type
        return self

    def with_formatter(self, name: str, **kwargs: Any) -> Self:
        """Set formatter by registered name."""
        formatter_cls = ProjectionRegistry.get_formatter(name)
        self._formatter = formatter_cls(**kwargs) if kwargs else formatter_cls()
        return self

    def with_formatter_instance(self, formatter: Formatter) -> Self:
        """Set pre-configured formatter instance."""
        self._formatter = formatter
        return self

    def to_sink(self, name: str, **kwargs: Any) -> Self:
        """Set sink by registered name."""
        sink_cls = ProjectionRegistry.get_sink(name)
        self._sink = sink_cls(**kwargs) if kwargs else sink_cls()
        return self

    def to_sink_instance(self, sink: SinkPort) -> Self:
        """Set pre-configured sink instance."""
        self._sink = sink
        return self

    def build(self) -> ProjectionPipeline:
        """Build configured pipeline with resolved defaults."""
        sink = self._sink
        if sink is None:
            sink_cls = ProjectionRegistry.get_sink("string")
            sink = sink_cls()

        return ProjectionPipeline(
            event_store=self._event_store,
            filter_=self._filter,
            formatter=self._formatter,
            sink=sink,
            output_type=self._output_type,
        )
