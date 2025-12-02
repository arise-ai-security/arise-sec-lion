"""Tests for ProjectionPipeline and ProjectionPipelineBuilder."""

from datetime import timedelta
from uuid import uuid4

import pytest

from core.application.projections.filters import ErrorOnlyFilter, IncludeAllFilter
from core.application.projections.formatters import JSONFormatter, JSONLinesFormatter
from core.application.projections.models import ProjectionSummary
from core.application.projections.pipeline import (
    ProjectionPipeline,
    ProjectionPipelineBuilder,
)
from core.application.projections.registry import RegistryError
from core.domain.events import (
    AgentCreated,
    ChildSpawned,
    DomainEvent,
    TaskAssigned,
    WorkCompleted,
    WorkFailed,
)
from core.domain.subtask import Subtask
from infrastructure.adapters.sinks import StringSink

from .conftest import BASE_TIME, BOSS_ID, MANAGER_ID, FakeEventStore


def _subtask_config() -> dict:
    """Standard config for test subtasks."""
    return {"strategy": "heuristic", "base": {"model": "test"}}


@pytest.fixture
def populated_event_store(fake_event_store) -> FakeEventStore:
    """Event store with sample hierarchy."""
    boss_events = [
        AgentCreated(
            aggregate_id=BOSS_ID,
            sequence_number=1,
            role="BOSS",
            parent_id=None,
            occurred_at=BASE_TIME,
        ),
        TaskAssigned(
            aggregate_id=BOSS_ID,
            sequence_number=2,
            task_description="Main task",
            occurred_at=BASE_TIME + timedelta(seconds=1),
        ),
        ChildSpawned(
            aggregate_id=BOSS_ID,
            sequence_number=3,
            child_id=MANAGER_ID,
            child_role="PENDING",
            subtask=Subtask(description="Subtask", config=_subtask_config()),
            child_config={},
            occurred_at=BASE_TIME + timedelta(seconds=2),
        ),
        WorkCompleted(
            aggregate_id=BOSS_ID,
            sequence_number=4,
            result="Done",
            occurred_at=BASE_TIME + timedelta(seconds=5),
        ),
    ]

    manager_events = [
        AgentCreated(
            aggregate_id=MANAGER_ID,
            sequence_number=1,
            role="MANAGER",
            parent_id=BOSS_ID,
            occurred_at=BASE_TIME + timedelta(seconds=3),
        ),
        WorkFailed(
            aggregate_id=MANAGER_ID,
            sequence_number=2,
            reason="Test failure",
            occurred_at=BASE_TIME + timedelta(seconds=4),
        ),
    ]

    fake_event_store.add_events(BOSS_ID, boss_events)
    fake_event_store.add_events(MANAGER_ID, manager_events)
    return fake_event_store


class TestProjectionPipeline:
    """Tests for ProjectionPipeline."""

    @pytest.mark.asyncio
    async def test_execute_writes_to_sink(self, populated_event_store) -> None:
        """Should write formatted output to sink."""
        sink = StringSink()
        pipeline = ProjectionPipeline(
            event_store=populated_event_store,
            filter_=IncludeAllFilter(),
            formatter=JSONFormatter(),
            sink=sink,
        )

        await pipeline.execute(BOSS_ID)

        output = sink.getvalue()
        assert output
        assert "AgentCreated" in output

    @pytest.mark.asyncio
    async def test_execute_applies_filter(self, populated_event_store) -> None:
        """Should apply filter to events."""
        sink = StringSink()
        pipeline = ProjectionPipeline(
            event_store=populated_event_store,
            filter_=ErrorOnlyFilter(),
            formatter=JSONFormatter(),
            sink=sink,
        )

        await pipeline.execute(BOSS_ID)

        output = sink.getvalue()
        assert "WorkFailed" in output
        assert "AgentCreated" not in output

    @pytest.mark.asyncio
    async def test_execute_summary_output(self, populated_event_store) -> None:
        """Should execute summary output."""
        sink = StringSink()
        pipeline = ProjectionPipeline(
            event_store=populated_event_store,
            filter_=IncludeAllFilter(),
            formatter=JSONFormatter(),
            sink=sink,
            output_type="summary",
        )

        await pipeline.execute(BOSS_ID)

        output = sink.getvalue()
        assert "total_events" in output
        assert "error_count" in output

    @pytest.mark.asyncio
    async def test_execute_events_returns_domain_events(self, populated_event_store) -> None:
        """execute_events should return DomainEvent list."""
        pipeline = ProjectionPipeline(
            event_store=populated_event_store,
            filter_=IncludeAllFilter(),
            formatter=JSONFormatter(),
            sink=StringSink(),
        )

        result = await pipeline.execute_events(BOSS_ID)

        assert isinstance(result, list)
        assert all(isinstance(e, DomainEvent) for e in result)
        assert len(result) == 6

    @pytest.mark.asyncio
    async def test_execute_summary_returns_summary(self, populated_event_store) -> None:
        """execute_summary should return ProjectionSummary."""
        pipeline = ProjectionPipeline(
            event_store=populated_event_store,
            filter_=IncludeAllFilter(),
            formatter=JSONFormatter(),
            sink=StringSink(),
        )

        result = await pipeline.execute_summary(BOSS_ID)

        assert isinstance(result, ProjectionSummary)
        assert result.total_events == 6

    @pytest.mark.asyncio
    async def test_execute_events_filtered(self, populated_event_store) -> None:
        """execute_events should return filtered DomainEvents."""
        pipeline = ProjectionPipeline(
            event_store=populated_event_store,
            filter_=ErrorOnlyFilter(),
            formatter=JSONFormatter(),
            sink=StringSink(),
        )

        result = await pipeline.execute_events(BOSS_ID)

        assert len(result) == 1
        assert isinstance(result[0], WorkFailed)


class TestProjectionPipelineBuilder:
    """Tests for ProjectionPipelineBuilder."""

    def test_build_with_defaults(self, fake_event_store) -> None:
        """Should build pipeline with sensible defaults."""
        pipeline = ProjectionPipelineBuilder(fake_event_store).build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_with_filter_by_name(self, fake_event_store) -> None:
        """Should set filter by registered name."""
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.with_filter("errors_only")
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_with_filter_with_kwargs(self, fake_event_store) -> None:
        """Should pass kwargs to filter constructor."""
        agent_id = uuid4()
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.with_filter("by_agent", agent_ids={agent_id})
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_with_filter_instance(self, fake_event_store) -> None:
        """Should accept pre-configured filter instance."""
        filter_ = ErrorOnlyFilter()
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.with_filter_instance(filter_)
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_with_output(self, fake_event_store) -> None:
        """Should set output type."""
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.with_output("summary")
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_with_formatter_by_name(self, fake_event_store) -> None:
        """Should set formatter by registered name."""
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.with_formatter("jsonl")
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_with_formatter_instance(self, fake_event_store) -> None:
        """Should accept pre-configured formatter instance."""
        formatter = JSONLinesFormatter()
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.with_formatter_instance(formatter)
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_to_sink_by_name(self, fake_event_store) -> None:
        """Should set sink by registered name."""
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.to_sink("string")
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_to_sink_instance(self, fake_event_store) -> None:
        """Should accept pre-configured sink instance."""
        sink = StringSink()
        builder = ProjectionPipelineBuilder(fake_event_store)
        builder.to_sink_instance(sink)
        pipeline = builder.build()

        assert isinstance(pipeline, ProjectionPipeline)

    def test_fluent_api_chaining(self, fake_event_store) -> None:
        """Should support fluent method chaining."""
        pipeline = (
            ProjectionPipelineBuilder(fake_event_store)
            .with_filter("errors_only")
            .with_output("events")
            .with_formatter("jsonl")
            .to_sink("string")
            .build()
        )

        assert isinstance(pipeline, ProjectionPipeline)

    def test_unknown_filter_raises_error(self, fake_event_store) -> None:
        """Should raise RegistryError for unknown filter."""
        builder = ProjectionPipelineBuilder(fake_event_store)

        with pytest.raises(RegistryError):
            builder.with_filter("nonexistent")

    def test_unknown_formatter_raises_error(self, fake_event_store) -> None:
        """Should raise RegistryError for unknown formatter."""
        builder = ProjectionPipelineBuilder(fake_event_store)

        with pytest.raises(RegistryError):
            builder.with_formatter("nonexistent")

    def test_unknown_sink_raises_error(self, fake_event_store) -> None:
        """Should raise RegistryError for unknown sink."""
        builder = ProjectionPipelineBuilder(fake_event_store)

        with pytest.raises(RegistryError):
            builder.to_sink("nonexistent")


class TestPipelineIntegration:
    """Integration tests for the full pipeline."""

    @pytest.mark.asyncio
    async def test_full_pipeline_with_jsonl_output(self, populated_event_store) -> None:
        """Should produce valid JSONL output."""
        sink = StringSink()
        pipeline = (
            ProjectionPipelineBuilder(populated_event_store)
            .with_filter("all")
            .with_formatter("jsonl")
            .to_sink_instance(sink)
            .build()
        )

        await pipeline.execute(BOSS_ID)

        output = sink.getvalue()
        lines = output.split("\n")

        assert len(lines) == 6

    @pytest.mark.asyncio
    async def test_full_pipeline_errors_only(self, populated_event_store) -> None:
        """Should filter to only errors."""
        sink = StringSink()
        pipeline = (
            ProjectionPipelineBuilder(populated_event_store)
            .with_filter("errors_only")
            .with_formatter("json")
            .to_sink_instance(sink)
            .build()
        )

        await pipeline.execute(BOSS_ID)

        output = sink.getvalue()
        import json

        parsed = json.loads(output)

        assert len(parsed) == 1
        assert parsed[0]["event_type"] == "WorkFailed"

    @pytest.mark.asyncio
    async def test_full_pipeline_summary_output(self, populated_event_store) -> None:
        """Should produce summary output."""
        sink = StringSink()
        pipeline = (
            ProjectionPipelineBuilder(populated_event_store)
            .with_filter("all")
            .with_output("summary")
            .with_formatter("json")
            .to_sink_instance(sink)
            .build()
        )

        await pipeline.execute(BOSS_ID)

        output = sink.getvalue()
        import json

        parsed = json.loads(output)

        assert parsed["total_events"] == 6
        assert parsed["error_count"] == 1
        assert len(parsed["agents_involved"]) == 2
