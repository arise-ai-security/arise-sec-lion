"""Runtime ports: protocols for LLM, worker, cost, context, and streaming.

Small port protocols consolidated into one file. EventStorePort stays
in its own file due to size and ISP split.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.events.events import DomainEvent
from core.domain.values.llm_response import LLMResponse, LLMToolResponse

if TYPE_CHECKING:
    from core.domain.shared_context import SharedStore
    from core.domain.values.node_message import Handoff


# =============================================================================
# LLM
# =============================================================================


class LLMPort(Protocol):
    """Query LLM with prompt and config."""

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str: ...

    async def query_with_usage(
        self, prompt: str, config_dict: dict[str, Any]
    ) -> LLMResponse: ...

    async def query_with_tools(
        self,
        messages: list[dict[str, Any]],
        config_dict: dict[str, Any],
        tools: list[dict[str, Any]],
    ) -> LLMToolResponse:
        """Send messages with tool definitions, return response with possible tool calls.

        Args:
            messages: Chat messages in OpenAI format (role/content dicts).
            config_dict: Model config (model, temperature, max_tokens).
            tools: Tool definitions in OpenAI function-calling format.

        Returns:
            LLMToolResponse with either content (final answer) or tool_calls.
        """
        ...


# =============================================================================
# Worker Tool
# =============================================================================


class WorkerToolPort(Protocol):
    """Stream events from worker tool execution (Claude Code, OpenHands)."""

    async def run_session(
        self, task_context: dict[str, Any]
    ) -> AsyncIterator[DomainEvent]:
        ...
        yield  # type: ignore


# =============================================================================
# Cost Calculator
# =============================================================================


class CostCalculatorPort(Protocol):
    """Calculate USD cost from token usage and model information."""

    def calculate_llm_cost(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float: ...

    def calculate_worker_cost(
        self,
        tool_name: str,
        model: str | None,
        tokens: int | None,
        duration_seconds: float,
    ) -> float: ...


# =============================================================================
# Real-time Streaming
# =============================================================================


class RealtimeCallbackPort(Protocol):
    """Stream events to external consumers (SSE) during worker execution."""

    async def on_event(self, event: DomainEvent, root_id: UUID) -> None: ...


# =============================================================================
# Shared Context
# =============================================================================


class SharedContextPort(Protocol):
    """Combined read/write interface for shared execution context."""

    async def get_or_create(
        self, root_id: UUID, config: dict | None = None
    ) -> "SharedStore": ...

    async def get(self, root_id: UUID) -> "SharedStore | None": ...

    async def save(
        self, context: "SharedStore", expected_version: int
    ) -> None: ...

    async def exists(self, root_id: UUID) -> bool: ...


# =============================================================================
# Sibling View
# =============================================================================


class SiblingViewPort(Protocol):
    """Build sibling view for worker coordination."""

    async def build_view(
        self, agent_id: UUID, parent_id: UUID | None, root_id: UUID
    ) -> "Handoff": ...


# =============================================================================
# Reconnaissance Tools (for manager/PENDING tool-calling)
# =============================================================================


class ReconToolPort(Protocol):
    """Read-only reconnaissance tools for manager assessment/decomposition.

    These allow PENDING/MANAGER agents to inspect the codebase before
    deciding whether to execute or decompose. All operations are read-only.
    """

    async def read_file(self, path: str, max_lines: int = 200) -> str:
        """Read file contents (truncated to max_lines)."""
        ...

    async def list_directory(self, path: str) -> str:
        """List directory entries with type indicators."""
        ...

    async def search_codebase(self, pattern: str, path: str = ".") -> str:
        """Search for a regex pattern in files (like grep -rn)."""
        ...

    async def find_file(self, pattern: str, path: str = ".") -> str:
        """Find files matching a glob pattern."""
        ...

    async def get_file_structure(self, path: str = ".", max_depth: int = 3) -> str:
        """Get a tree view of the directory structure."""
        ...

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format."""
        ...

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call by name and return the result as a string."""
        ...
