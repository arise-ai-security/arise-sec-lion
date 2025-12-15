"""Event Projection System for CQRS read-side.

This package provides a pipeline for transforming domain events into
structured reports (JSON logs, summaries) with filtering capabilities.

Architecture:
    EventStore -> HierarchyCollector -> Filter -> Formatter -> Sink

Components:
    - models: Data structures (ProjectionSummary)
    - registry: Plugin-like registration system for extensibility
    - base: Abstract base classes and protocols (Projection, EventFilter, Formatter)
    - filters: Event filtering strategies (all, errors_only, by_agent)
    - impl: Projection implementations (SummaryProjection)
    - formatters: Output formatting (JSON, JSONL, Text)
    - hierarchy_collector: Recursive event collection from agent hierarchy
    - pipeline: Fluent builder for pipeline construction

Note on Sinks (Hexagonal Architecture):
    ALL sink implementations are in infrastructure layer:
        from infrastructure.adapters.sinks import StdoutSink, FileSink, StringSink

    The SinkPort protocol (abstract interface) is in core/ports/sink_port.py.
"""

from core.query.ports.sink_port import SinkPort
from core.query.projections.base import (
    EventFilter,
    Formatter,
    Projection,
)
from core.query.projections.filters import (
    AgentFilter,
    AnyOfFilter,
    CompositeFilter,
    ErrorOnlyFilter,
    EventTypeFilter,
    IncludeAllFilter,
)
from core.query.projections.formatters import (
    CompactTextFormatter,
    JSONFormatter,
    JSONLinesFormatter,
    TextFormatter,
)
from core.query.projections.hierarchy_collector import HierarchyCollector
from core.query.projections.impl import SummaryProjection
from core.query.projections.models import ProjectionSummary
from core.query.projections.pipeline import (
    ProjectionPipeline,
    ProjectionPipelineBuilder,
)
from core.query.projections.registry import (
    ProjectionRegistry,
    RegistryError,
    register_filter,
    register_formatter,
    register_projection,
    register_sink,
)


__all__ = [
    "AgentFilter",
    "AnyOfFilter",
    "CompactTextFormatter",
    "CompositeFilter",
    "ErrorOnlyFilter",
    "EventFilter",
    "EventTypeFilter",
    "Formatter",
    "HierarchyCollector",
    "IncludeAllFilter",
    "JSONFormatter",
    "JSONLinesFormatter",
    "Projection",
    "ProjectionPipeline",
    "ProjectionPipelineBuilder",
    "ProjectionRegistry",
    "ProjectionSummary",
    "RegistryError",
    "SinkPort",
    "SummaryProjection",
    "TextFormatter",
    "register_filter",
    "register_formatter",
    "register_projection",
    "register_sink",
]
