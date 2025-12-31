"""Output formatter implementations: JSON, JSONL, text, compact."""

import json
from collections.abc import Callable
from typing import Any

from core.domain.events.events import DomainEvent
from core.query.projections.models import ProjectionSummary
from core.query.projections.registry import register_formatter


def _event_to_dict(event: DomainEvent) -> dict[str, Any]:
    """Convert DomainEvent to JSON-serializable dict via Pydantic."""
    data = event.model_dump(mode="json")
    data["event_type"] = type(event).__name__
    return data


def _summary_to_dict(summary: ProjectionSummary) -> dict[str, Any]:
    return {
        "total_events": summary.total_events,
        "events_by_type": summary.events_by_type,
        "agents_involved": [str(agent_id) for agent_id in summary.agents_involved],
        "first_event": summary.first_event.isoformat() if summary.first_event else None,
        "last_event": summary.last_event.isoformat() if summary.last_event else None,
        "error_count": summary.error_count,
        "errors": [_event_to_dict(e) for e in summary.errors],
    }


@register_formatter("json")
class JSONFormatter:
    """Pretty-printed JSON array."""

    def __init__(self, indent: int = 2) -> None:
        self._indent = indent

    def format(self, events: list[DomainEvent]) -> str:
        data = [_event_to_dict(event) for event in events]
        return json.dumps(data, indent=self._indent, ensure_ascii=False)

    def format_summary(self, summary: ProjectionSummary) -> str:
        data = _summary_to_dict(summary)
        return json.dumps(data, indent=self._indent, ensure_ascii=False)


@register_formatter("jsonl")
class JSONLinesFormatter:
    """One JSON object per line (streaming-friendly)."""

    def format(self, events: list[DomainEvent]) -> str:
        lines = [json.dumps(_event_to_dict(event), ensure_ascii=False) for event in events]
        return "\n".join(lines)

    def format_summary(self, summary: ProjectionSummary) -> str:
        data = _summary_to_dict(summary)
        return json.dumps(data, ensure_ascii=False)


@register_formatter("text")
class TextFormatter:
    """Human-readable multi-line text."""

    def format(self, events: list[DomainEvent]) -> str:
        lines = [self._format_event(event) for event in events]
        return "\n".join(lines)

    def format_summary(self, summary: ProjectionSummary) -> str:
        lines = [
            "=== Projection Summary ===",
            f"Total Events: {summary.total_events}",
            f"Agents Involved: {len(summary.agents_involved)}",
            "",
            "Events by Type:",
        ]
        for event_type, count in sorted(summary.events_by_type.items()):
            lines.append(f"  {event_type}: {count}")

        if summary.first_event and summary.last_event:
            lines.append("")
            lines.append(f"First Event: {summary.first_event.isoformat()}")
            lines.append(f"Last Event: {summary.last_event.isoformat()}")

        if summary.errors:
            lines.append("")
            lines.append(f"Errors ({summary.error_count}):")
            for error in summary.errors:
                lines.append(
                    f"  - [{error.occurred_at.isoformat()}] {getattr(error, 'reason', 'Unknown')}"
                )

        return "\n".join(lines)

    def _format_event(self, event: DomainEvent) -> str:
        timestamp_str = event.occurred_at.strftime("%Y-%m-%d %H:%M:%S")
        event_type = type(event).__name__
        lines = [
            f"[{timestamp_str}] {event_type}",
            f"Agent: {event.aggregate_id}",
        ]

        details = self._get_event_details(event)
        if details:
            for key, value in details.items():
                lines.append(f"  {key}: {value}")

        lines.append("---")
        return "\n".join(lines)

    def _get_event_details(self, event: DomainEvent) -> dict[str, Any]:
        base_fields = {"event_id", "aggregate_id", "sequence_number", "occurred_at", "metadata"}
        details = {}
        for field_name in type(event).model_fields:
            if field_name not in base_fields:
                value = getattr(event, field_name)
                if isinstance(value, str) and len(value) > 100:
                    value = value[:100] + "..."
                details[field_name] = value
        return details


@register_formatter("compact")
class CompactTextFormatter:
    """Single-line per event."""

    def format(self, events: list[DomainEvent]) -> str:
        lines = [self._format_event(event) for event in events]
        return "\n".join(lines)

    def format_summary(self, summary: ProjectionSummary) -> str:
        type_counts = ", ".join(f"{t}:{c}" for t, c in sorted(summary.events_by_type.items()))
        return (
            f"Events: {summary.total_events} | "
            f"Agents: {len(summary.agents_involved)} | "
            f"Errors: {summary.error_count} | "
            f"Types: [{type_counts}]"
        )

    def _format_event(self, event: DomainEvent) -> str:
        time_str = event.occurred_at.strftime("%H:%M:%S")
        agent_short = str(event.aggregate_id)[:8]
        event_type = type(event).__name__

        summary = self._get_event_summary(event)
        max_len = 80
        if len(summary) > max_len:
            summary = summary[:max_len] + "..."

        return f"{time_str} {event_type} [{agent_short}] {summary}"

    def _get_event_summary(self, event: DomainEvent) -> str:
        return _EVENT_SUMMARY_HANDLERS.get(type(event).__name__, _default_summary)(event)


def _default_summary(_event: DomainEvent) -> str:
    return ""


def _agent_created_summary(event: DomainEvent) -> str:
    return f"role={getattr(event, 'role', '')}"


def _task_assigned_summary(event: DomainEvent) -> str:
    return f"task={getattr(event, 'task_description', '')[:50]}"


def _status_changed_summary(event: DomainEvent) -> str:
    old = getattr(event, "old_status", "")
    new = getattr(event, "new_status", "")
    return f"{old} -> {new}"


def _complexity_evaluated_summary(event: DomainEvent) -> str:
    comp = getattr(event, "complexity", "")
    role = getattr(event, "determined_role", "")
    return f"{comp} -> {role}"


def _subtasks_defined_summary(event: DomainEvent) -> str:
    return f"{len(getattr(event, 'subtasks', []))} subtasks"


def _child_spawned_summary(event: DomainEvent) -> str:
    cid = str(getattr(event, "child_id", ""))[:8]
    crole = getattr(event, "child_role", "")
    return f"child={cid} role={crole}"


def _work_completed_summary(event: DomainEvent) -> str:
    return getattr(event, "result", "")[:50]


def _work_failed_summary(event: DomainEvent) -> str:
    return getattr(event, "reason", "")[:50]


def _thought_captured_summary(event: DomainEvent) -> str:
    stream = getattr(event, "stream", "")
    content = getattr(event, "content", "")[:30]
    return f"[{stream}] {content}"


def _code_generation_started_summary(event: DomainEvent) -> str:
    return f"tool={getattr(event, 'tool_name', '')}"


def _child_completed_summary(event: DomainEvent) -> str:
    return f"child={str(getattr(event, 'child_id', ''))[:8]}"


_EVENT_SUMMARY_HANDLERS: dict[str, Callable[[DomainEvent], str]] = {
    "AgentCreated": _agent_created_summary,
    "TaskAssigned": _task_assigned_summary,
    "StatusChanged": _status_changed_summary,
    "ComplexityEvaluated": _complexity_evaluated_summary,
    "SubtasksDefined": _subtasks_defined_summary,
    "ChildSpawned": _child_spawned_summary,
    "WorkCompleted": _work_completed_summary,
    "WorkFailed": _work_failed_summary,
    "ThoughtCaptured": _thought_captured_summary,
    "CodeGenerationStarted": _code_generation_started_summary,
    "ChildCompleted": _child_completed_summary,
}
