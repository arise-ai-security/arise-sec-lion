"""OpenHands SDK-event conversion, streaming, and result formatting.

Pure module functions that turn OpenHands SDK conversation events into domain
events and compact result transcripts. No adapter or session state — each
function keys off the SDK event / conversation objects it is handed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.domain.values.node_message import WORKER_CONCLUSION_MARKER

from .openhands_container_tools import replace_host_paths


if TYPE_CHECKING:
    from collections.abc import Iterator

    from core.domain.events.events import DomainEvent

    from .shared import ContainerSessionContext, EventSequencer


SDK_EVENT_TYPE_MAP: dict[str, str] = {
    "ActionEvent": "tool_use",
    "AgentThinkAction": "thinking",
    "MessageEvent": "output",
    "ObservationEvent": "tool_result",
    "CmdRunObservation": "tool_result",
    "AgentErrorEvent": "output",
    "FileReadAction": "tool_use",
    "FileWriteAction": "tool_use",
    "FileEditAction": "tool_use",
}

_EVENT_TEXT_LIMIT = 600


def iter_conversation_events(conversation: Any) -> list[Any]:
    state = getattr(conversation, "state", None)
    if state is None or not hasattr(state, "events"):
        return []
    return list(state.events)


def iter_conversation_domain_events(
    conversation: Any,
    sequencer: EventSequencer,
) -> Iterator[DomainEvent]:
    for sdk_event in iter_conversation_events(conversation):
        if thought_event := sdk_event_to_thought(sdk_event, sequencer):
            yield thought_event


def extract_finish_message(conversation: Any) -> str | None:
    for sdk_event in iter_conversation_events(conversation):
        finish_message = _try_extract_finish(sdk_event)
        if finish_message is not None:
            return finish_message
    return None


def sdk_event_to_thought(
    sdk_event: Any,
    sequencer: EventSequencer,
) -> DomainEvent | None:
    content = extract_event_content(sdk_event)
    if not content or not content.strip():
        return None

    output_type = classify_event(sdk_event)
    tool_name = extract_tool_name(sdk_event) if output_type == "tool_use" else None
    return sequencer.thought(content.strip(), output_type, tool_name=tool_name)


def make_completed_event(
    conversation: Any,
    working_dir: str,
    finish_message: str | None,
    sequencer: EventSequencer,
    container_session: ContainerSessionContext | None = None,
) -> DomainEvent:
    result = extract_result(conversation, working_dir, finish_message)
    if container_session is not None:
        # Observations are sanitized at the tool layer, but transcript tails
        # that bypass it (e.g. directory views) can still carry host paths
        # into the result — and from there into sibling prompts.
        result = replace_host_paths(result, container_session)
    return sequencer.completed(result)


def extract_result(
    conversation: Any,
    working_dir: str,
    finish_message: str | None = None,
) -> str:
    """Build a compact command log from ALL conversation events.

    Instead of dumping raw SDK repr for a few events, this creates a
    structured ``$ command (exit N)`` + truncated output summary for
    every output event. The verification judge can then see the full
    execution timeline at a glance.
    """
    entries: list[str] = []
    for event in iter_conversation_events(conversation):
        if classify_event(event) not in {"output", "tool_result"}:
            continue
        summary = _compact_observation(event)
        if summary:
            entries.append(summary)

    parts: list[str] = []
    if entries:
        parts.extend(entries)
    if finish_message:
        parts.append(f"{WORKER_CONCLUSION_MARKER}\n{finish_message}")
    if parts:
        return "\n".join(parts)
    return f"Task completed in workspace: {working_dir}"


def classify_event(event: Any) -> str:
    if getattr(event, "action", None) is not None:
        return "tool_use"
    if getattr(event, "observation", None) is not None:
        return "tool_result"
    event_class_name = type(event).__name__
    return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")


def extract_event_content(event: Any) -> str | None:
    if hasattr(event, "action") and event.action:
        return _format_action_event(event.action)
    if hasattr(event, "observation") and event.observation:
        return _format_observation_event(event.observation)
    if hasattr(event, "message") and event.message:
        return str(event.message)
    if hasattr(event, "content") and event.content:
        return str(event.content)
    if hasattr(event, "thought") and event.thought:
        return f"Thought: {event.thought}"
    return None


def extract_tool_name(event: Any) -> str | None:
    action = getattr(event, "action", None)
    if action is None:
        return None
    return type(action).__name__ or "OpenHandsAction"


def _compact_observation(event: Any) -> str | None:
    """Extract command, exit code, and truncated text from an SDK event."""
    obs = getattr(event, "observation", None)
    if obs is None:
        raw = extract_event_content(event)
        if not raw:
            return None
        return _truncate_text(raw, _EVENT_TEXT_LIMIT)

    cmd = getattr(obs, "command", "") or ""
    metadata = getattr(obs, "metadata", None)
    exit_code = getattr(metadata, "exit_code", None) if metadata else None
    text = observation_text(obs)

    header = f"$ {cmd} (exit {exit_code})" if cmd else ""
    if text:
        text = _truncate_text(text, _EVENT_TEXT_LIMIT)

    if header and text:
        return f"{header}\n{text}"
    return header or text or None


def observation_text(obs: Any) -> str:
    content = getattr(obs, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            text = getattr(item, "text", None)
            if text:
                return str(text)
    return str(content)


def _truncate_text(text: str, limit: int) -> str:
    """Head+tail truncation of a single text block."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[...{len(text) - limit} chars truncated...]\n{text[-half:]}"


def _try_extract_finish(event: Any) -> str | None:
    action = getattr(event, "action", None)
    if action is None:
        return None
    if type(action).__name__ != "FinishAction":
        return None
    msg = getattr(action, "message", None)
    return str(msg).strip() if msg and str(msg).strip() else None


def _format_action_event(action: Any) -> str:
    tool_name = type(action).__name__ or "OpenHandsAction"
    payload: dict[str, Any] = {}
    for attr_name in ("command", "path", "file_path", "thought", "content"):
        value = getattr(action, attr_name, None)
        if value:
            payload[attr_name] = str(value)
    detail = payload if payload else {"repr": _truncate_text(str(action), _EVENT_TEXT_LIMIT)}
    return f"Tool: {tool_name}\nInput: {json_dumps_for_event(detail)}"


def _format_observation_event(observation: Any) -> str:
    command = getattr(observation, "command", "") or ""
    metadata = getattr(observation, "metadata", None)
    exit_code = getattr(metadata, "exit_code", None) if metadata else None
    text = _truncate_text(observation_text(observation), _EVENT_TEXT_LIMIT)
    parts = []
    if command:
        parts.append(f"command={command}")
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    header = "; ".join(parts) or type(observation).__name__
    return f"Tool result: {header}\n{text}".rstrip()


def json_dumps_for_event(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, default=str)
