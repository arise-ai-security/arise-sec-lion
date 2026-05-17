"""Family-specific metrics over authoritative DB events."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from experiments.shared.scripts.db_event_queries import DbEvent
from experiments.shared.scripts.db_study_inputs import CohortEntry
from experiments.shared.scripts.run_metrics import metrics_from_events


_INPUT_MARKER = "\nInput: "
_TOOL_PREFIX = re.compile(r"^Tool:\s*([A-Za-z0-9_.:-]+)")
_VERDICT_HEADER = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:final\s+)?(?:verdict|result|assessment)\s*[:\-]"
)
_PHASE_PATTERNS: dict[str, re.Pattern[str]] = {
    "builder": re.compile(r"\b(builder|build|reproducer|repro|crash|trigger)\b", re.I),
    "exploiter": re.compile(r"\b(exploit|exploiter|poc|proof)\b", re.I),
    "fixer": re.compile(r"\b(fix|fixer|patch|diff|mitigation|regression)\b", re.I),
}
_PROVIDER_FAILURE = re.compile(
    r"\b(api|provider|rate.?limit|429|quota|overload|anthropic|openai|litellm)\b",
    re.I,
)
_TIMEOUT_NO_OUTPUT = re.compile(r"\b(timeout|timed out|no output|empty output)\b", re.I)


class FamilyAggregator(Protocol):
    """Aggregate metrics for a study family."""

    family: str

    def aggregate(self, cohort: CohortEntry, events: list[DbEvent]) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class AggregationRegistry:
    """Lookup family aggregators by manifest cell group."""

    aggregators: dict[str, FamilyAggregator]

    @classmethod
    def default(cls) -> "AggregationRegistry":
        return cls(
            aggregators={
                "A": AFamilyAggregator(),
                "B": BFamilyAggregator(),
                "C": CFamilyAggregator(),
            }
        )

    def for_family(self, family: str) -> FamilyAggregator:
        try:
            return self.aggregators[family]
        except KeyError as exc:
            known = ", ".join(sorted(self.aggregators))
            raise ValueError(f"no DB metric aggregator for family {family!r}; known: {known}") from exc


class AFamilyAggregator:
    """Claude Code flat-run metrics for A-family cells."""

    family = "A"

    def aggregate(self, cohort: CohortEntry, events: list[DbEvent]) -> dict[str, Any]:
        common = _common_metric_row(cohort, events, self.family)
        tool_events = [_tool_event(event) for event in events]
        tool_events = [event for event in tool_events if event is not None]
        tools = Counter(event.tool_name for event in tool_events)

        task_create = sum(1 for event in tool_events if event.tool_name in {"Task", "TaskCreate"})
        task_update = tools.get("TaskUpdate", 0)
        task_list = tools.get("TaskList", 0)
        phase_counts = _phase_evidence_counts(tool_events)
        verdict_headers = _verdict_headers(tool_events)

        common.update(
            {
                "claude_tool_use_count": len(tool_events),
                "bash_tool_call_count": tools.get("Bash", 0),
                "write_tool_call_count": tools.get("Write", 0),
                "edit_tool_call_count": tools.get("Edit", 0) + tools.get("MultiEdit", 0),
                "task_create_count": task_create,
                "task_update_count": task_update,
                "task_list_count": task_list,
                "task_tool_call_count": task_create + task_update + task_list,
                "phase_deliverable_evidence_count": sum(phase_counts.values()),
                "builder_deliverable_evidence": phase_counts["builder"],
                "exploiter_deliverable_evidence": phase_counts["exploiter"],
                "fixer_deliverable_evidence": phase_counts["fixer"],
                "verdict_header_count": len(verdict_headers),
                "verdict_headers_json": json.dumps(verdict_headers[:20], sort_keys=True),
            }
        )
        return common


class BFamilyAggregator:
    """Hierarchy and recon metrics for B-family cells."""

    family = "B"

    def aggregate(self, cohort: CohortEntry, events: list[DbEvent]) -> dict[str, Any]:
        common = _common_metric_row(cohort, events, self.family)
        roles = Counter(
            str(event.payload.get("role") or "").lower()
            for event in events
            if event.event_type == "AgentCreated"
        )
        common.update(
            {
                "worker_tool_call_count": sum(
                    1
                    for event in events
                    if event.event_type == "ThoughtCaptured"
                    and event.payload.get("output_type") == "tool_use"
                ),
                "recon_probe_started_count": sum(
                    1 for event in events if event.event_type == "ProbeStarted"
                ),
                "recon_probe_completed_count": sum(
                    1 for event in events if event.event_type == "ProbeCompleted"
                ),
                "agent_created_count": sum(
                    1 for event in events if event.event_type == "AgentCreated"
                ),
                "boss_agent_count": roles.get("boss", 0),
                "manager_agent_count": roles.get("manager", 0),
                "worker_agent_count": roles.get("worker", 0),
                "child_spawned_count": sum(
                    1 for event in events if event.event_type == "ChildSpawned"
                ),
                "judge_event_count": sum(
                    1
                    for event in events
                    if event.event_type in {"VerificationPassed", "VerificationFailed"}
                ),
            }
        )
        return common


class CFamilyAggregator:
    """OpenHands-oriented metrics and censoring indicators for C-family cells."""

    family = "C"

    def aggregate(self, cohort: CohortEntry, events: list[DbEvent]) -> dict[str, Any]:
        common = _common_metric_row(cohort, events, self.family)
        tool_events = [_tool_event(event) for event in events]
        tool_events = [event for event in tool_events if event is not None]
        failure_text = "\n".join(
            str(event.payload.get("reason") or "")
            for event in events
            if event.event_type == "WorkFailed"
        )
        worker_cost_count = sum(1 for event in events if event.event_type == "WorkerCostRecorded")
        total_worker_cost = sum(
            _float(event.payload.get("cost_usd"))
            for event in events
            if event.event_type == "WorkerCostRecorded"
        )
        common.update(
            {
                "openhands_tool_call_count": len(tool_events),
                "openhands_bash_action_count": sum(
                    1
                    for event in tool_events
                    if event.tool_name in {"ExecuteBashAction", "CmdRunAction", "Bash"}
                ),
                "openhands_file_action_count": sum(
                    1
                    for event in tool_events
                    if "File" in event.tool_name or event.tool_name in {"Read", "Write", "Edit"}
                ),
                "cost_missing_indicator": int(worker_cost_count == 0),
                "cost_censored_indicator": int(worker_cost_count > 0 and total_worker_cost == 0.0),
                "iteration_failure_count": int("iteration" in failure_text.lower()),
                "timeout_no_output_failure_count": int(bool(_TIMEOUT_NO_OUTPUT.search(failure_text))),
            }
        )
        return common


@dataclass(frozen=True)
class ToolEvent:
    """Parsed worker tool-use event."""

    tool_name: str
    input_payload: dict[str, Any]
    content: str


def is_provider_failure(reason: str) -> bool:
    return bool(_PROVIDER_FAILURE.search(reason))


def is_timeout_or_no_output(reason: str) -> bool:
    return bool(_TIMEOUT_NO_OUTPUT.search(reason))


def _common_metric_row(
    cohort: CohortEntry,
    events: list[DbEvent],
    family: str,
) -> dict[str, Any]:
    metrics = metrics_from_events([event.as_metric_event() for event in events])
    row: dict[str, Any] = {
        "source_authority": "db_events",
        "run_id": cohort.run_id,
        "cell": cohort.cell,
        "task": cohort.task,
        "replicate": cohort.replicate,
        "family": family,
    }
    row.update(metrics)
    return row


def _tool_event(event: DbEvent) -> ToolEvent | None:
    if event.event_type != "ThoughtCaptured":
        return None
    if event.payload.get("output_type") != "tool_use":
        return None
    content = str(event.payload.get("content") or "")
    input_payload = _parse_tool_input(content)
    return ToolEvent(
        tool_name=_canonical_tool_name(event.payload, content, input_payload),
        input_payload=input_payload,
        content=content,
    )


def _parse_tool_input(content: str) -> dict[str, Any]:
    idx = content.rfind(_INPUT_MARKER)
    if idx < 0:
        return {}
    try:
        parsed = json.loads(content[idx + len(_INPUT_MARKER):])
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical_tool_name(
    payload: dict[str, Any],
    content: str,
    input_payload: dict[str, Any],
) -> str:
    structured = payload.get("tool_name")
    if isinstance(structured, str) and structured.strip():
        return structured.strip()

    first_line = content.splitlines()[0] if content else ""
    match = _TOOL_PREFIX.match(first_line)
    if match:
        return match.group(1)
    if first_line.startswith("Running:") or "command" in input_payload:
        return "Bash"
    if first_line.startswith("Writing:") or (
        "content" in input_payload and "file_path" in input_payload
    ):
        return "Write"
    if first_line.startswith("Editing:") or "old_string" in input_payload:
        return "Edit"
    if "edits" in input_payload:
        return "MultiEdit"
    if first_line.startswith("Reading:"):
        return "Read"
    if first_line.startswith("Searching files:"):
        return "Glob"
    if first_line.startswith("Searching content:"):
        return "Grep"
    return "unknown"


def _phase_evidence_counts(tool_events: list[ToolEvent]) -> Counter[str]:
    counts: Counter[str] = Counter({"builder": 0, "exploiter": 0, "fixer": 0})
    for event in tool_events:
        if event.tool_name not in {"Write", "Edit", "MultiEdit", "Bash"}:
            continue
        haystack = _tool_evidence_text(event)
        for phase, pattern in _PHASE_PATTERNS.items():
            if pattern.search(haystack):
                counts[phase] += 1
    return counts


def _tool_evidence_text(event: ToolEvent) -> str:
    pieces = [event.content]
    for key in ("file_path", "path", "content", "command", "old_string", "new_string"):
        value = event.input_payload.get(key)
        if isinstance(value, str):
            pieces.append(value)
    edits = event.input_payload.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                pieces.extend(str(value) for value in edit.values() if isinstance(value, str))
    return "\n".join(pieces)


def _verdict_headers(tool_events: list[ToolEvent]) -> list[str]:
    headers: list[str] = []
    for event in tool_events:
        if event.tool_name != "Write":
            continue
        content = event.input_payload.get("content")
        if not isinstance(content, str):
            continue
        headers.extend(match.group(0).strip() for match in _VERDICT_HEADER.finditer(content))
    return headers


def _float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
