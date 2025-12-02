"""Projection pipeline with fluent builder API.

This module provides the main orchestration for the projection system,
combining all components into a cohesive pipeline.

Usage:
    pipeline = (
        ProjectionPipelineBuilder(event_store)
        .with_filter("errors_only")
        .with_formatter("jsonl")
        .to_sink("stdout")
        .build()
    )
    await pipeline.execute(boss_agent_id)
"""

from typing import Any, Self
from uuid import UUID

from core.application.projections.base import EventFilter, Formatter
from core.application.projections.filters import IncludeAllFilter
from core.application.projections.formatters import JSONFormatter
from core.application.projections.hierarchy_collector import HierarchyCollector
from core.application.projections.impl import SummaryProjection
from core.application.projections.models import ProjectionSummary
from core.application.projections.registry import ProjectionRegistry
from core.domain.events import DomainEvent
from core.ports.event_store_port import EventStorePort
from core.ports.sink_port import SinkPort


class ProjectionPipeline:
    """Orchestrates the full projection pipeline.

    The pipeline executes the following steps:
    1. Collect events from the hierarchy (HierarchyCollector)
    2. Filter events (EventFilter)
    3. Format output to string (Formatter)
    4. Write to destination (Sink)

    For summaries, an additional aggregation step is performed.

    Attributes:
        event_store: The event store for reading events.
        filter: The event filter to apply.
        formatter: The formatter for output.
        sink: The output destination.
    """

    def __init__(
        self,
        event_store: EventStorePort,
        filter_: EventFilter,
        formatter: Formatter,
        sink: SinkPort,
        output_type: str = "events",
    ) -> None:
        """Initialize the pipeline with all components.

        Args:
            event_store: Event store for reading events.
            filter_: Filter to apply to events.
            formatter: Formatter for output.
            sink: Output destination.
            output_type: Type of output ("events" or "summary").
        """
        self._event_store = event_store
        self._filter = filter_
        self._formatter = formatter
        self._sink = sink
        self._output_type = output_type
        self._collector = HierarchyCollector(event_store)

    async def execute(self, root_agent_id: UUID) -> None:
        """Execute the full pipeline for an agent hierarchy.

        Args:
            root_agent_id: UUID of the root agent (typically BOSS).
        """
        # Step 1: Collect events from hierarchy
        events = await self._collector.collect(root_agent_id)

        # Step 2: Filter events
        filtered = [e for e in events if self._filter.matches(e)]

        # Step 3: Format and output
        if self._output_type == "summary":
            projection = SummaryProjection()
            summary = projection.project(filtered)
            output = self._formatter.format_summary(summary)
        else:
            output = self._formatter.format(filtered)

        # Step 4: Write to sink
        self._sink.write(output)

    async def execute_events(self, root_agent_id: UUID) -> list[DomainEvent]:
        """Execute collection and filtering only, return raw events.

        Useful for custom processing of filtered events.

        Args:
            root_agent_id: UUID of the root agent.

        Returns:
            List of filtered DomainEvent records.
        """
        events = await self._collector.collect(root_agent_id)
        return [e for e in events if self._filter.matches(e)]

    async def execute_summary(self, root_agent_id: UUID) -> ProjectionSummary:
        """Execute pipeline and return summary (no sink output).

        Useful when you need the summary data rather than formatted output.

        Args:
            root_agent_id: UUID of the root agent.

        Returns:
            ProjectionSummary with aggregated statistics.
        """
        events = await self._collector.collect(root_agent_id)
        filtered = [e for e in events if self._filter.matches(e)]
        projection = SummaryProjection()
        return projection.project(filtered)


class ProjectionPipelineBuilder:
    """Fluent builder for constructing projection pipelines.

    Provides a chainable API for configuring pipeline components.

    Usage:
        pipeline = (
            ProjectionPipelineBuilder(event_store)
            .with_filter("errors_only")
            .with_formatter("jsonl")
            .to_sink("stdout")  # Requires infrastructure sinks to be imported
            .build()
        )

    Default values:
        - Filter: "all" (IncludeAllFilter)
        - Output: "events" (list of events)
        - Formatter: "json" (JSONFormatter)
        - Sink: "string" (StringSink - captures output in memory)

    Note:
        For I/O sinks (stdout, file), import infrastructure.adapters.sinks
        to register them, then use to_sink("stdout") or to_sink("file").
    """

    def __init__(self, event_store: EventStorePort) -> None:
        """Initialize with an event store.

        Args:
            event_store: The event store for reading events.
        """
        self._event_store = event_store
        self._filter: EventFilter = IncludeAllFilter()
        self._output_type: str = "events"
        self._formatter: Formatter = JSONFormatter()
        # Default sink resolved via registry (avoids core->infrastructure import)
        self._sink: SinkPort | None = None

    def with_filter(self, name: str, **kwargs: Any) -> Self:
        """Set the filter by registered name.

        Args:
            name: Registered filter name (e.g., "all", "errors_only").
            **kwargs: Arguments to pass to the filter constructor.

        Returns:
            Self for method chaining.

        Raises:
            RegistryError: If filter name is not registered.
        """
        filter_cls = ProjectionRegistry.get_filter(name)
        self._filter = filter_cls(**kwargs) if kwargs else filter_cls()
        return self

    def with_filter_instance(self, filter_: EventFilter) -> Self:
        """Set a pre-configured filter instance.

        Use this when you need to configure the filter manually
        or use a composite filter.

        Args:
            filter_: The filter instance to use.

        Returns:
            Self for method chaining.
        """
        self._filter = filter_
        return self

    def with_output(self, output_type: str) -> Self:
        """Set the output type.

        Args:
            output_type: Output type ("events" or "summary").

        Returns:
            Self for method chaining.
        """
        self._output_type = output_type
        return self

    def with_formatter(self, name: str, **kwargs: Any) -> Self:
        """Set the formatter by registered name.

        Args:
            name: Registered formatter name (e.g., "json", "jsonl", "text").
            **kwargs: Arguments to pass to the formatter constructor.

        Returns:
            Self for method chaining.

        Raises:
            RegistryError: If formatter name is not registered.
        """
        formatter_cls = ProjectionRegistry.get_formatter(name)
        self._formatter = formatter_cls(**kwargs) if kwargs else formatter_cls()
        return self

    def with_formatter_instance(self, formatter: Formatter) -> Self:
        """Set a pre-configured formatter instance.

        Args:
            formatter: The formatter instance to use.

        Returns:
            Self for method chaining.
        """
        self._formatter = formatter
        return self

    def to_sink(self, name: str, **kwargs: Any) -> Self:
        """Set the sink by registered name.

        Args:
            name: Registered sink name (e.g., "stdout", "file", "string").
            **kwargs: Arguments to pass to the sink constructor.

        Returns:
            Self for method chaining.

        Raises:
            RegistryError: If sink name is not registered.
        """
        sink_cls = ProjectionRegistry.get_sink(name)
        self._sink = sink_cls(**kwargs) if kwargs else sink_cls()
        return self

    def to_sink_instance(self, sink: SinkPort) -> Self:
        """Set a pre-configured sink instance.

        Args:
            sink: The sink instance to use.

        Returns:
            Self for method chaining.
        """
        self._sink = sink
        return self

    def build(self) -> ProjectionPipeline:
        """Build the configured pipeline.

        Returns:
            A configured ProjectionPipeline ready for execution.

        Raises:
            RegistryError: If default "string" sink is not registered.
        """
        # Resolve default sink via registry if not explicitly set
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
