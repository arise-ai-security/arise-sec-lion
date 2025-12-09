"""Composite Worker Tool Adapter - Routes to appropriate tool based on config.

This adapter implements the WorkerToolPort interface and routes task execution
to the appropriate underlying adapter (Claude Code or OpenHands) based on the
tool_name specified in the agent's configuration.

Architecture Note:
    This is the "Strategy Pattern" for worker tool selection. The parent agent
    decides which tool to use (config.tool), and this composite adapter routes
    to the correct implementation.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from core.domain.events import DomainEvent
from core.domain.exceptions import ToolNotAvailableError
from core.ports.worker_port import WorkerToolPort


logger = logging.getLogger(__name__)


class CompositeWorkerAdapter(WorkerToolPort):
    """Worker tool adapter that routes to the appropriate tool based on config.

    This adapter holds references to multiple worker tool implementations
    (Claude Code, OpenHands) and dispatches to the correct one based on
    the tool_name in the task context.

    Attributes:
        adapters: Dictionary mapping tool names to their adapter implementations.
        default_tool: Default tool to use if none specified.
    """

    def __init__(
        self,
        adapters: dict[str, WorkerToolPort],
        default_tool: str = "claude_code",
    ) -> None:
        """Initialize the composite adapter with tool implementations.

        Args:
            adapters: Dictionary mapping tool names to WorkerToolPort implementations.
                     Expected keys: "claude_code", "openhands"
            default_tool: Default tool to use if task_context doesn't specify one.
        """
        self.adapters = adapters
        self.default_tool = default_tool

    async def run_session(self, task_context: dict[str, Any]) -> AsyncIterator[DomainEvent]:
        """Execute a task by routing to the appropriate worker tool.

        Examines task_context["tool_name"] to determine which adapter to use.
        Falls back to default_tool if not specified.

        Args:
            task_context: Context for task execution. Expected keys:
                - tool_name: str (e.g., "claude_code", "openhands")
                - task_description: str
                - session_id: UUID
                - working_directory: str (optional)

        Yields:
            DomainEvent instances from the underlying adapter.

        Raises:
            ValueError: If the specified tool is not available.
        """
        # Get tool name from context or use default
        tool_name = task_context.get("tool_name", self.default_tool)

        # Normalize tool name (handle variations)
        tool_key = self._normalize_tool_name(tool_name)

        # Check if tool is available
        if tool_key not in self.adapters:
            available = list(self.adapters.keys())
            logger.error(
                f"Tool '{tool_name}' (key: '{tool_key}') unavailable. Options: {available}"
            )
            # Raise exception - domain layer will emit proper WorkFailed event
            raise ToolNotAvailableError(tool_name, available)

        # Get the appropriate adapter and delegate
        adapter = self.adapters[tool_key]
        logger.info(f"Routing task to '{tool_key}' adapter")

        # Stream events from the underlying adapter
        async for event in adapter.run_session(task_context):
            yield event

    def _normalize_tool_name(self, tool_name: str) -> str:
        """Normalize tool name to match adapter keys.

        Handles variations like:
        - "claude_code" -> "claude_code"
        - "claude-code" -> "claude_code"
        - "ClaudeCode" -> "claude_code"
        - "openhands" -> "openhands"
        - "open_hands" -> "openhands"

        Args:
            tool_name: Raw tool name from config.

        Returns:
            Normalized tool name matching adapter keys.
        """
        # Convert to lowercase and replace separators
        normalized = tool_name.lower().replace("-", "_").replace(" ", "_")

        # Handle specific variations
        if normalized in ("claude_code", "claudecode"):
            return "claude_code"
        if normalized in ("openhands", "open_hands"):
            return "openhands"

        # Return as-is if no specific handling
        return normalized
