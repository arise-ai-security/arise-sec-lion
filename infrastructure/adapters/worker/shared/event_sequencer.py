"""Event sequencer for worker adapters.

Tracks sequence numbers and creates domain events with proper sequencing.
"""

from typing import Any
from uuid import UUID

from core.domain.events.events import (
    ThoughtCaptured,
    WorkCompleted,
    WorkerCostRecorded,
    WorkerUsageMetrics,
    WorkFailed,
)


def _sanitize_for_jsonb(text: str) -> str:
    """Remove characters that PostgreSQL JSONB cannot store.

    PostgreSQL rejects null bytes (\\x00 / \\u0000) inside JSONB strings.
    Worker terminal output (e.g. large tool output, binary data)
    can contain these bytes, causing UntranslatableCharacterError on insert.
    """
    return text.replace("\x00", "")


def _sanitize_dict_for_jsonb(value: Any) -> Any:
    """Recursively strip null bytes from string leaves of a dict/list/scalar.

    PostgreSQL rejects \\x00 inside JSONB strings. Tool inputs (Bash commands,
    Grep patterns, file paths) can carry stray nulls from binary tool outputs
    that flow back as arguments -- sanitize the same way strings are sanitized
    in _sanitize_for_jsonb.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: _sanitize_dict_for_jsonb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_dict_for_jsonb(v) for v in value]
    return value


class EventSequencer:
    """Tracks sequence numbers and creates domain events.

    Encapsulates the repetitive pattern of creating events with incrementing
    sequence numbers. Thread-safe for single-task use (not concurrent).

    Example:
        sequencer = EventSequencer(agent_id, stream="claude_sdk")
        yield sequencer.thought("Processing...", "thinking")
        yield sequencer.thought("Running command", "progress")
        yield sequencer.completed("Done!")
    """

    def __init__(self, agent_id: UUID, stream: str, start_sequence: int = 2) -> None:
        """Initialize sequencer.

        Args:
            agent_id: The aggregate ID for all events.
            stream: The stream identifier (e.g., "claude_sdk", "openhands").
            start_sequence: Starting sequence number (default 2, since 1 is
                reserved for CodeGenerationStarted in the domain).
        """
        self._agent_id = agent_id
        self._stream = stream
        self._sequence = start_sequence

    @property
    def current_sequence(self) -> int:
        """Return current sequence number (for testing/debugging)."""
        return self._sequence

    def thought(
        self,
        content: str,
        output_type: str,
        call_id: str | None = None,
        duration_ms: int | None = None,
        was_truncated: bool = False,
        result_bytes: int | None = None,
        tool_input_json: dict[str, Any] | None = None,
        tool_name: str | None = None,
    ) -> ThoughtCaptured:
        """Create a ThoughtCaptured event and increment sequence.

        Args:
            content: The thought content to capture.
            output_type: Type classification (thinking, output, progress, etc.).
            call_id: Optional tool invocation id (matches SDK ``tool_use_id``)
                used to correlate pre/post tool use hooks.
            duration_ms: Optional per-tool-call duration in milliseconds,
                computed from the matching PreToolUse start timestamp.
            was_truncated: True if ``content`` was cut at the capture cap.
            result_bytes: Original byte length of ``content`` before any capping
                was applied. Populated whenever the adapter applied the
                truncation cap (both ``tool_result`` from the Claude SDK adapter
                and all thought events from the OpenHands adapter). None when
                the adapter does not expose a content length.
            tool_input_json: Structured tool-call arguments captured verbatim so
                downstream analysis does not need to re-parse the formatter
                string (None when the adapter cannot expose structured input).
                Recursively sanitized for JSONB-incompatible null bytes before
                being attached to the event.
            tool_name: SDK-reported tool identifier (e.g. ``Read``, ``Bash``).
                None for non-tool events or adapters that lack structured names.

        Returns:
            ThoughtCaptured event with current sequence number.
        """
        event = ThoughtCaptured(
            aggregate_id=self._agent_id,
            sequence_number=self._sequence,
            content=_sanitize_for_jsonb(content),
            stream=self._stream,
            output_type=output_type,
            call_id=call_id,
            duration_ms=duration_ms,
            was_truncated=was_truncated,
            result_bytes=result_bytes,
            tool_input_json=(
                _sanitize_dict_for_jsonb(tool_input_json)
                if tool_input_json is not None
                else None
            ),
            tool_name=tool_name,
        )
        self._sequence += 1
        return event

    def completed(self, result: str) -> WorkCompleted:
        """Create a WorkCompleted event (terminal, no sequence increment).

        Args:
            result: The completion result message.

        Returns:
            WorkCompleted event with current sequence number.
        """
        return WorkCompleted(
            aggregate_id=self._agent_id,
            sequence_number=self._sequence,
            result=_sanitize_for_jsonb(result),
        )

    def failed(self, reason: str) -> WorkFailed:
        """Create a WorkFailed event (terminal, no sequence increment).

        Args:
            reason: The failure reason message.

        Returns:
            WorkFailed event with current sequence number.
        """
        return WorkFailed(
            aggregate_id=self._agent_id,
            sequence_number=self._sequence,
            reason=_sanitize_for_jsonb(reason),
        )

    def cost_recorded(
        self,
        tool_name: str,
        cost_usd: float,
        duration_seconds: float,
        model: str | None = None,
        tokens: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        usage_metrics: list[WorkerUsageMetrics] | None = None,
    ) -> WorkerCostRecorded:
        """Create a WorkerCostRecorded event and increment sequence.

        Args:
            tool_name: Worker tool name (e.g., "claude_code", "openhands").
            cost_usd: Total cost in USD.
            duration_seconds: Execution duration.
            model: Underlying model if known.
            tokens: Total tokens if available.
            prompt_tokens: Prompt/input tokens if available.
            completion_tokens: Completion/output tokens if available.
            cache_read_tokens: Cache read tokens if available.
            cache_write_tokens: Cache write tokens if available.
            reasoning_tokens: Reasoning tokens if available.
            usage_metrics: Detailed per-usage worker SDK metrics if available.

        Returns:
            WorkerCostRecorded event with current sequence number.
        """
        event = WorkerCostRecorded(
            aggregate_id=self._agent_id,
            sequence_number=self._sequence,
            tool_name=tool_name,
            model=model,
            tokens=tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_metrics=usage_metrics or [],
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
        )
        self._sequence += 1
        return event
