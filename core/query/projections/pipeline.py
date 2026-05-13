"""Projection pipeline: collect → filter → format → sink."""

from typing import Any, Self
from uuid import UUID

from core.domain.events.events import DomainEvent
from core.ports.event_store_port import EventStoreReadPort
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
        event_store: EventStoreReadPort,
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
        events = await self._collector.collect(root_agent_id)
        return [e for e in events if self._filter.matches(e)]

    async def execute_summary(self, root_agent_id: UUID) -> ProjectionSummary:
        events = await self._collector.collect(root_agent_id)
        filtered = [e for e in events if self._filter.matches(e)]
        projection = SummaryProjection()
        return projection.project(filtered)


class ProjectionPipelineBuilder:
    """Fluent builder for constructing projection pipelines."""

    def __init__(self, event_store: EventStoreReadPort) -> None:
        self._event_store = event_store
        self._filter: EventFilter = IncludeAllFilter()
        self._output_type: str = "events"
        self._formatter: Formatter = JSONFormatter()
        self._sink: SinkPort | None = None

    def with_filter(self, name: str, **kwargs: Any) -> Self:
        filter_cls = ProjectionRegistry.get_filter(name)
        self._filter = filter_cls(**kwargs) if kwargs else filter_cls()
        return self

    def with_filter_instance(self, filter_: EventFilter) -> Self:
        self._filter = filter_
        return self

    def with_output(self, output_type: str) -> Self:
        self._output_type = output_type
        return self

    def with_formatter(self, name: str, **kwargs: Any) -> Self:
        formatter_cls = ProjectionRegistry.get_formatter(name)
        self._formatter = formatter_cls(**kwargs) if kwargs else formatter_cls()
        return self

    def with_formatter_instance(self, formatter: Formatter) -> Self:
        self._formatter = formatter
        return self

    def to_sink(self, name: str, **kwargs: Any) -> Self:
        sink_cls = ProjectionRegistry.get_sink(name)
        self._sink = sink_cls(**kwargs) if kwargs else sink_cls()
        return self

    def to_sink_instance(self, sink: SinkPort) -> Self:
        self._sink = sink
        return self

    def build(self) -> ProjectionPipeline:
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
