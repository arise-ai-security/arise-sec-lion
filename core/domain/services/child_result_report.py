"""Render parent-facing text summaries of child results and failures.

Pure formatting over aggregate state, kept out of AgentSession so the
aggregate changes only for invariant reasons and the report wording can
change without touching replay logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from uuid import UUID

    from core.domain.values.failure import ChildFailureRecord
    from core.domain.values.node_message import Report


_NOTE_TASK_CHARS = 80
_NOTE_REASON_CHARS = 200


def aggregate_child_results(
    child_ids: list[UUID],
    child_reports: dict[UUID, Report],
) -> str:
    lines = ["Subtask results:", ""]
    for child_id in child_ids:
        report = child_reports.get(child_id)
        if report is None:
            lines.append("- No result")
            continue
        header = f"[{report.task}]" if report.task else "[subtask]"
        lines.append(f"- {header}: {report.result}")
        if report.artifacts:
            lines.append(f"  Artifacts: {', '.join(report.artifacts)}")
        if report.decisions:
            lines.append(f"  Decisions: {', '.join(report.decisions)}")
    return "\n".join(lines)


def _child_failure_lines(failed_children: dict[UUID, ChildFailureRecord]) -> list[str]:
    lines = []
    for record in failed_children.values():
        label = record.child_task[:_NOTE_TASK_CHARS] or str(record.child_id)[:8]
        first_line = record.reason.splitlines()[0] if record.reason else ""
        lines.append(f"- {label}: {first_line[:_NOTE_REASON_CHARS]}")
    return lines


def failed_children_note(failed_children: dict[UUID, ChildFailureRecord]) -> str:
    header = f"Note: {len(failed_children)} child(ren) failed. Results are partial."
    return "\n".join([header, *_child_failure_lines(failed_children)])


def all_children_failed_reason(failed_children: dict[UUID, ChildFailureRecord]) -> str:
    header = f"All {len(failed_children)} children failed:"
    return "\n".join([header, *_child_failure_lines(failed_children)])
