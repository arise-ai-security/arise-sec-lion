"""Worker Tool port: execute tasks via Claude Code or OpenHands."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from core.domain.events import DomainEvent


class WorkerToolPort(Protocol):
    """Stream events from worker tool execution (Claude Code, OpenHands)."""

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Execute task and yield events (progress, output, completion/failure)."""
        ...
        yield  # type: ignore
