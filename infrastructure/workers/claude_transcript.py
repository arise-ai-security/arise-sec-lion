"""Parse Claude Code CLI ``stream-json`` transcripts into domain events.

Pure transcript -> ``DomainEvent`` translation extracted from
``ClaudeCodeWorker``: turns the ``claude -p --output-format stream-json`` JSONL
log into thought/tool/cost events, and best-effort summarizes the terminal
``result`` line for ``WorkerResult``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from infrastructure.adapters.worker.shared import (
    EventSequencer,
    UsageBreakdown,
    emit_cost,
    format_tool_event,
)


if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from core.domain.events.events import DomainEvent


def events_from_transcript(
    transcript_path: Path,
    run_id: UUID,
    wall_time_seconds: float,
    model: str | None,
) -> list[DomainEvent]:
    """Parse Claude Code stream-json into event-store-compatible events."""
    sequencer = EventSequencer(run_id, stream="claude_code")
    events: list[DomainEvent] = []
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return events

    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            events.append(sequencer.thought(line, "output"))
            continue
        if not isinstance(payload, dict):
            continue
        events.extend(_message_events(payload, sequencer))
        event = cost_event(payload, sequencer, wall_time_seconds, model)
        if event is not None:
            events.append(event)
    return events


def _message_events(
    payload: dict[str, object],
    sequencer: EventSequencer,
) -> list[DomainEvent]:
    out: list[DomainEvent] = []
    message = payload.get("message")
    blocks: object = None
    if isinstance(message, dict):
        blocks = message.get("content")
    if blocks is None:
        blocks = payload.get("content")
    if not isinstance(blocks, list):
        return out

    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"text", "output"}:
            text = _string_value(block.get("text") or block.get("content"))
            if text:
                out.append(sequencer.thought(text, "output"))
        elif block_type == "thinking":
            text = _string_value(block.get("thinking") or block.get("text"))
            if text:
                out.append(sequencer.thought(text, "thinking"))
        elif block_type == "tool_use":
            tool_name = str(block.get("name") or "unknown")
            raw_input = block.get("input")
            tool_input = raw_input if isinstance(raw_input, dict) else {}
            formatted = format_tool_event(tool_name, tool_input)
            detail = json.dumps(tool_input, sort_keys=True, default=str)
            # Audit N-6 / prefixes.py: pass the canonical tool_name through
            # to ThoughtCaptured so downstream metrics no longer have to
            # parse the rendered content prefix to recover it. Mirrors the
            # B-family SDK path (claude_sdk_adapter.py:289).
            out.append(
                sequencer.thought(
                    f"{formatted}\nInput: {detail}",
                    "tool_use",
                    tool_name=tool_name,
                )
            )
        elif block_type == "tool_result":
            content = _string_value(block.get("content"))
            out.append(
                sequencer.thought(
                    f"Tool result: {content or '(no output)'}",
                    "tool_result",
                )
            )
    return out


def cost_event(
    payload: dict[str, object],
    sequencer: EventSequencer,
    wall_time_seconds: float,
    model: str | None,
) -> DomainEvent | None:
    if payload.get("type") != "result":
        return None
    usage = payload.get("usage")
    usage_dict = usage if isinstance(usage, dict) else {}
    prompt_tokens = _int_value(usage_dict.get("input_tokens") or usage_dict.get("prompt_tokens"))
    completion_tokens = _int_value(
        usage_dict.get("output_tokens") or usage_dict.get("completion_tokens")
    )
    cache_read_tokens = _int_value(usage_dict.get("cache_read_input_tokens"))
    cache_write_tokens = _int_value(usage_dict.get("cache_creation_input_tokens"))
    reasoning_tokens = _int_value(
        usage_dict.get("reasoning_tokens") or usage_dict.get("thinking_tokens")
    )
    breakdown = UsageBreakdown(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=_float_value(payload.get("total_cost_usd") or payload.get("cost_usd")),
    )
    if not breakdown.has_cost_data:
        return None
    duration = _float_value(payload.get("duration_ms"))
    duration_seconds = (duration / 1000.0) if duration is not None else wall_time_seconds
    model = _string_value(payload.get("model")) or model
    return emit_cost(
        sequencer,
        tool_name="claude_code",
        model=model,
        duration_seconds=duration_seconds,
        breakdown=breakdown,
    )


def summarize_transcript(transcript_path: Path) -> str | None:
    """Best-effort summary of the captured stdout for ``WorkerResult``.

    Reads the last line of stream-json (typically the terminal ``result``
    message). Falls back to ``None`` when parsing fails — the full
    transcript is on disk for downstream tools.
    """
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last_line: str | None = None
    for line in text.splitlines():
        if line.strip():
            last_line = line.strip()
    if last_line is None:
        return None
    try:
        parsed = json.loads(last_line)
    except json.JSONDecodeError:
        return last_line[:500] or None
    if isinstance(parsed, dict):
        for field in ("result", "output", "content", "text"):
            value = parsed.get(field)
            if isinstance(value, str) and value:
                return value[:500]
    return None


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_string_value(item) for item in value if item is not None)
    if value is None:
        return ""
    return str(value)


def _int_value(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_value(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
