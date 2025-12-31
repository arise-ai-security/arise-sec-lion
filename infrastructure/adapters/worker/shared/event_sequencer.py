"""Event sequencer for worker adapters.

Tracks sequence numbers and creates domain events with proper sequencing.
"""

from uuid import UUID

from core.domain.events.events import ThoughtCaptured, WorkCompleted, WorkFailed, WorkerCostRecorded


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

    def thought(self, content: str, output_type: str) -> ThoughtCaptured:
        """Create a ThoughtCaptured event and increment sequence.

        Args:
            content: The thought content to capture.
            output_type: Type classification (thinking, output, progress, etc.).

        Returns:
            ThoughtCaptured event with current sequence number.
        """
        event = ThoughtCaptured(
            aggregate_id=self._agent_id,
            sequence_number=self._sequence,
            content=content,
            stream=self._stream,
            output_type=output_type,
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
            result=result,
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
            reason=reason,
        )

    def cost_recorded(
        self,
        tool_name: str,
        cost_usd: float,
        duration_seconds: float,
        model: str | None = None,
        tokens: int | None = None,
    ) -> WorkerCostRecorded:
        """Create a WorkerCostRecorded event and increment sequence.

        Args:
            tool_name: Worker tool name (e.g., "claude_code", "openhands").
            cost_usd: Total cost in USD.
            duration_seconds: Execution duration.
            model: Underlying model if known.
            tokens: Total tokens if available.

        Returns:
            WorkerCostRecorded event with current sequence number.
        """
        event = WorkerCostRecorded(
            aggregate_id=self._agent_id,
            sequence_number=self._sequence,
            tool_name=tool_name,
            model=model,
            tokens=tokens,
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
        )
        self._sequence += 1
        return event
