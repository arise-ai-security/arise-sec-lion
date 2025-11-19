"""Worker Tool port definition.

This module defines the abstract interface for worker tools that execute
leaf-level tasks. Infrastructure adapters (e.g., Claude Code PTY, OpenHands PTY)
must implement this protocol.
"""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from core.domain.events import DomainEvent


class WorkerToolPort(Protocol):
    """Abstract interface for worker tool execution with real-time event streaming.

    Worker tools are "black box" executors (Claude Code, OpenHands) that run
    tasks and emit their thinking process. Implementations must wrap these tools
    in PTY (pseudo-terminal) adapters to capture stdout/stderr in real-time.

    The streaming approach allows:
    - Real-time monitoring of worker progress
    - Incremental event persistence
    - Early detection of failures or blocks
    """

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Execute a worker task and stream domain events as they occur.

        This method wraps the external tool (Claude Code/OpenHands) in a PTY,
        captures its output, and yields domain events representing:
        - Task start/progress/completion
        - Thinking logs (stdout/stderr capture)
        - Errors or blocks

        Args:
            task_context: Context for the task execution. Common keys include:
                - task_description: str (the task to execute)
                - session_id: UUID (the agent session ID)
                - tool_name: str (e.g., "claude-code", "openhands")
                - working_directory: str
                - timeout_seconds: int
                - environment: dict[str, str]

        Yields:
            DomainEvent instances as the task progresses. Events may include:
            - TaskStartedEvent
            - ThinkingLogCapturedEvent
            - TaskCompletedEvent
            - TaskFailedEvent
            - TaskBlockedEvent

        Raises:
            WorkerToolError: On tool execution failures or timeouts.
            ValidationError: If task_context is missing required fields.
        """
        ...
        # Required for Protocol: yield statement to indicate AsyncIterator
        yield  # type: ignore
