"""Tests for output formatters."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.domain.events.events import AgentCreated, WorkFailed
from core.query.projections.formatters import (
    CompactTextFormatter,
    JSONFormatter,
    JSONLinesFormatter,
    TextFormatter,
)
from core.query.projections.models import ProjectionSummary
from core.query.projections.registry import ProjectionRegistry


@pytest.fixture
def sample_events() -> list:
    """Create sample events for testing."""
    agent_id = uuid4()
    timestamp = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
    return [
        AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role="BOSS",
            parent_id=None,
            config={"model": "test"},
            occurred_at=timestamp,
        ),
        WorkFailed(
            aggregate_id=agent_id,
            sequence_number=2,
            reason="Test error occurred",
            occurred_at=timestamp,
        ),
    ]


@pytest.fixture
def sample_summary(sample_events) -> ProjectionSummary:
    """Create sample summary for testing."""
    agent_id = sample_events[0].aggregate_id
    return ProjectionSummary(
        total_events=10,
        events_by_type={"AgentCreated": 5, "WorkFailed": 5},
        agents_involved=frozenset({agent_id}),
        first_event=sample_events[0].occurred_at,
        last_event=sample_events[1].occurred_at,
        error_count=1,
        errors=(sample_events[1],),
    )


class TestJSONFormatter:
    """Tests for JSONFormatter."""

    def test_registered_as_json(self) -> None:
        """Should be registered as 'json'."""
        fmt_cls = ProjectionRegistry.get_formatter("json")
        assert fmt_cls is JSONFormatter

    def test_format_returns_valid_json(self, sample_events) -> None:
        """Should return valid JSON array."""
        formatter = JSONFormatter()
        result = formatter.format(sample_events)

        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_format_includes_event_type(self, sample_events) -> None:
        """Should include event_type field."""
        formatter = JSONFormatter()
        result = formatter.format(sample_events)

        parsed = json.loads(result)
        assert parsed[0]["event_type"] == "AgentCreated"
        assert parsed[1]["event_type"] == "WorkFailed"

    def test_format_includes_all_fields(self, sample_events) -> None:
        """Should include all event fields."""
        formatter = JSONFormatter()
        result = formatter.format(sample_events)

        parsed = json.loads(result)
        entry = parsed[0]

        assert "event_id" in entry
        assert "aggregate_id" in entry
        assert "sequence_number" in entry
        assert "occurred_at" in entry
        assert "role" in entry  # AgentCreated-specific

    def test_format_serializes_datetime_as_iso(self, sample_events) -> None:
        """Should serialize datetime as ISO format."""
        formatter = JSONFormatter()
        result = formatter.format(sample_events)

        parsed = json.loads(result)
        assert "2024-01-15" in parsed[0]["occurred_at"]

    def test_format_summary_returns_valid_json(self, sample_summary) -> None:
        """Should format summary as valid JSON."""
        formatter = JSONFormatter()
        result = formatter.format_summary(sample_summary)

        parsed = json.loads(result)
        assert parsed["total_events"] == 10
        assert parsed["error_count"] == 1

    def test_custom_indent(self, sample_events) -> None:
        """Should support custom indentation."""
        formatter = JSONFormatter(indent=4)
        result = formatter.format(sample_events)

        assert "    " in result


class TestJSONLinesFormatter:
    """Tests for JSONLinesFormatter."""

    def test_registered_as_jsonl(self) -> None:
        """Should be registered as 'jsonl'."""
        fmt_cls = ProjectionRegistry.get_formatter("jsonl")
        assert fmt_cls is JSONLinesFormatter

    def test_format_returns_one_json_per_line(self, sample_events) -> None:
        """Should output one JSON object per line."""
        formatter = JSONLinesFormatter()
        result = formatter.format(sample_events)

        lines = result.split("\n")
        assert len(lines) == 2

        for line in lines:
            parsed = json.loads(line)
            assert isinstance(parsed, dict)

    def test_format_no_trailing_newline(self, sample_events) -> None:
        """Should not have trailing newline."""
        formatter = JSONLinesFormatter()
        result = formatter.format(sample_events)

        assert not result.endswith("\n")

    def test_format_summary_single_line(self, sample_summary) -> None:
        """Should format summary as single JSON line."""
        formatter = JSONLinesFormatter()
        result = formatter.format_summary(sample_summary)

        assert "\n" not in result

        parsed = json.loads(result)
        assert parsed["total_events"] == 10


class TestTextFormatter:
    """Tests for TextFormatter."""

    def test_registered_as_text(self) -> None:
        """Should be registered as 'text'."""
        fmt_cls = ProjectionRegistry.get_formatter("text")
        assert fmt_cls is TextFormatter

    def test_format_includes_timestamp(self, sample_events) -> None:
        """Should include formatted timestamp."""
        formatter = TextFormatter()
        result = formatter.format(sample_events)

        assert "2024-01-15 10:30:00" in result

    def test_format_includes_event_type(self, sample_events) -> None:
        """Should include event type."""
        formatter = TextFormatter()
        result = formatter.format(sample_events)

        assert "AgentCreated" in result
        assert "WorkFailed" in result

    def test_format_includes_separator(self, sample_events) -> None:
        """Should include separator between entries."""
        formatter = TextFormatter()
        result = formatter.format(sample_events)

        assert "---" in result

    def test_format_summary_includes_totals(self, sample_summary) -> None:
        """Should include total counts in summary."""
        formatter = TextFormatter()
        result = formatter.format_summary(sample_summary)

        assert "Total Events: 10" in result
        assert "Agents Involved: 1" in result


class TestCompactTextFormatter:
    """Tests for CompactTextFormatter."""

    def test_registered_as_compact(self) -> None:
        """Should be registered as 'compact'."""
        fmt_cls = ProjectionRegistry.get_formatter("compact")
        assert fmt_cls is CompactTextFormatter

    def test_format_single_line_per_entry(self, sample_events) -> None:
        """Should output one line per entry."""
        formatter = CompactTextFormatter()
        result = formatter.format(sample_events)

        lines = result.split("\n")
        assert len(lines) == 2

    def test_format_includes_short_agent_id(self, sample_events) -> None:
        """Should include truncated agent ID."""
        formatter = CompactTextFormatter()
        result = formatter.format(sample_events)

        short_id = str(sample_events[0].aggregate_id)[:8]
        assert short_id in result

    def test_format_summary_compact(self, sample_summary) -> None:
        """Should format summary as compact single line."""
        formatter = CompactTextFormatter()
        result = formatter.format_summary(sample_summary)

        assert "Events: 10" in result
        assert "Errors: 1" in result
