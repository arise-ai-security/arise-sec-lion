"""Composite Worker Adapter: route to Claude Code or OpenHands based on config."""

import logging
from collections.abc import AsyncIterator
from typing import Any

from core.domain.events import DomainEvent
from core.domain.exceptions import ToolNotAvailableError
from core.ports.worker_port import WorkerToolPort


logger = logging.getLogger(__name__)


class CompositeWorkerAdapter(WorkerToolPort):
    """Route tasks to appropriate worker tool based on tool_name in config."""

    def __init__(
        self,
        adapters: dict[str, WorkerToolPort],
        default_tool: str = "claude_code",
    ) -> None:
        self.adapters = adapters
        self.default_tool = default_tool

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Route to adapter based on tool_name, yield events from underlying adapter."""
        tool_name = task_context.get("tool_name", self.default_tool)
        tool_key = self._normalize_tool_name(tool_name)

        if tool_key not in self.adapters:
            available = list(self.adapters.keys())
            logger.error(f"Tool '{tool_name}' unavailable. Options: {available}")
            raise ToolNotAvailableError(tool_name, available)

        adapter = self.adapters[tool_key]
        logger.info(f"Routing task to '{tool_key}' adapter")

        async for event in adapter.run_session(task_context):
            yield event

    def _normalize_tool_name(self, tool_name: str) -> str:
        """Normalize variations: claude-code→claude_code, open_hands→openhands."""
        normalized = tool_name.lower().replace("-", "_").replace(" ", "_")
        if normalized in ("claude_code", "claudecode"):
            return "claude_code"
        if normalized in ("openhands", "open_hands"):
            return "openhands"
        return normalized
