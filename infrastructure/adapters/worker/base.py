"""Base class for worker adapters.

Provides shared infrastructure while keeping adapter-specific logic in subclasses.
Only truly shared behavior belongs here.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from core.domain.events.events import DomainEvent, WorkCompleted, WorkFailed
from core.ports.runtime_ports import WorkerToolPort

from .shared import EventSequencer, validate_task_context
from .shared.errors import describe_error


class WorkerAdapterBase(ABC, WorkerToolPort):
    """Base class with ONLY shared behavior for worker adapters.

    Subclasses MUST define:
        STREAM_NAME: str - Stream identifier for EventSequencer (e.g., "claude_sdk")

    Subclasses MUST implement:
        _execute_task(): Core execution logic
        _get_tool_name(): Tool identifier for cost tracking

    Provides shared helpers:
        _create_sequencer(): Create EventSequencer with STREAM_NAME

    Adapter-specific logic (error formatting, cost extraction, event processing)
    stays in the respective subclass. Each adapter owns its own per-call timing
    (local ``started_at = time()`` inside ``_execute_task``) so concurrent
    sessions of the same adapter instance never share a clock.
    """

    STREAM_NAME: str  # Subclass must define

    def __init__(self, timeout_seconds: int = 300) -> None:
        """Initialize base adapter.

        Args:
            timeout_seconds: Default timeout for task execution.
        """
        self.timeout_seconds = timeout_seconds

    async def run_session(
        self, task_context: dict[str, Any]
    ) -> AsyncIterator[DomainEvent]:
        """Execute task and yield domain events.

        Template method:
        1. Validate context
        2. Create sequencer
        3. Delegate to _execute_task()

        SDK-specific error handling stays in subclasses. This wrapper is only
        a final safety net so unexpected subclass escapes still produce a
        terminal worker event.
        """
        task_description, agent_id, working_dir = validate_task_context(task_context)
        sequencer = self._create_sequencer(agent_id)
        terminal_emitted = False

        try:
            async for event in self._execute_task(
                task_description=task_description,
                agent_id=agent_id,
                working_dir=working_dir,
                sequencer=sequencer,
                task_context=task_context,
            ):
                if isinstance(event, (WorkCompleted, WorkFailed)):
                    terminal_emitted = True
                yield event
        except Exception as error:
            if not terminal_emitted:
                yield sequencer.failed(self._format_unexpected_error(error))

    @abstractmethod
    async def _execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        """Execute task and yield events.

        Subclass must:
        - Record ``started_at = time()`` locally at the beginning so concurrent
          calls do not share state via the adapter instance
        - Yield ThoughtCaptured events during execution
        - Call sequencer.cost_recorded() with cost data, passing
          ``duration_seconds=time() - started_at``
        - Yield WorkCompleted or WorkFailed as final event
        - Handle errors with SDK-specific formatting

        Args:
            task_description: The task to execute.
            agent_id: Agent aggregate ID.
            working_dir: Working directory for execution.
            sequencer: EventSequencer for creating domain events.

        Yields:
            Domain events (ThoughtCaptured, WorkerCostRecorded, WorkCompleted/WorkFailed).
        """
        yield  # type: ignore

    @abstractmethod
    def _get_tool_name(self) -> str:
        """Return tool identifier for cost tracking.

        Examples: "claude_code", "openhands", "google_adk"
        """
        ...

    def _format_unexpected_error(self, error: Exception) -> str:
        return f"{self._get_tool_name()} adapter error: {describe_error(error)}"

    def _create_sequencer(self, agent_id: UUID) -> EventSequencer:
        return EventSequencer(agent_id, stream=self.STREAM_NAME)
