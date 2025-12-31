"""Base class for worker adapters.

Provides shared infrastructure while keeping adapter-specific logic in subclasses.
Only truly shared behavior belongs here.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from time import time
from typing import Any
from uuid import UUID

from core.domain.events.events import DomainEvent
from core.ports.worker_port import WorkerToolPort

from .shared import EventSequencer, validate_task_context


class WorkerAdapterBase(ABC, WorkerToolPort):
    """Base class with ONLY shared behavior for worker adapters.

    Subclasses MUST define:
        STREAM_NAME: str - Stream identifier for EventSequencer (e.g., "claude_sdk")

    Subclasses MUST implement:
        _execute_task(): Core execution logic
        _get_tool_name(): Tool identifier for cost tracking

    Provides shared helpers:
        _create_sequencer(): Create EventSequencer with STREAM_NAME
        _start_timing() / _get_duration(): Timing helpers for cost tracking

    Adapter-specific logic (error formatting, cost extraction, event processing)
    stays in the respective subclass.
    """

    STREAM_NAME: str  # Subclass must define

    def __init__(self, timeout_seconds: int = 300) -> None:
        """Initialize base adapter.

        Args:
            timeout_seconds: Default timeout for task execution.
        """
        self.timeout_seconds = timeout_seconds
        self._start_time: float = 0.0

    async def run_session(
        self, task_context: dict[str, Any]
    ) -> AsyncIterator[DomainEvent]:
        """Execute task and yield domain events.

        Template method:
        1. Validate context
        2. Create sequencer
        3. Delegate to _execute_task()

        Error handling is left to subclasses for SDK-specific formatting.
        """
        task_description, agent_id, working_dir = validate_task_context(task_context)
        sequencer = self._create_sequencer(agent_id)

        async for event in self._execute_task(
            task_description=task_description,
            agent_id=agent_id,
            working_dir=working_dir,
            sequencer=sequencer,
        ):
            yield event

    @abstractmethod
    async def _execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
    ) -> AsyncIterator[DomainEvent]:
        """Execute task and yield events.

        Subclass must:
        - Call self._start_timing() at the beginning
        - Yield ThoughtCaptured events during execution
        - Call sequencer.cost_recorded() with cost data
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

    def _create_sequencer(self, agent_id: UUID) -> EventSequencer:
        """Create EventSequencer tagged with this adapter's STREAM_NAME."""
        return EventSequencer(agent_id, stream=self.STREAM_NAME)

    def _start_timing(self) -> None:
        """Record start time for duration tracking.

        Call at the beginning of _execute_task().
        """
        self._start_time = time()

    def _get_duration(self) -> float:
        """Get elapsed seconds since _start_timing().

        Use when calling sequencer.cost_recorded().
        """
        return time() - self._start_time if self._start_time else 0.0
