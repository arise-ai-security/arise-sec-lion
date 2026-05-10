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

    async def reconnect(
        self,
        *,
        model: str | None = None,
        config_dict: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> bool:
        """Best-effort provider/session reconnect.

        This is an infrastructure recovery hook. It must not mutate
        agent/task semantics; callers use it before escalating to
        agent-level retries.
        """
        ...


# =============================================================================
# Output Format Repairer (LLM-backed fallback for malformed model output)
# =============================================================================
#
# NOTE: Distinct from the security-domain "Fixer" agent role. This port
# repairs the *output format* of LLM responses — never source code.


class FormatRepairerPort(Protocol):
    """Repair malformed JSON-shaped LLM output by sending it to a repair model.

    Used as the second-tier fallback after the deterministic
    ``strip_markdown_code_block`` / ``raw_decode`` / ``_repair_json`` chain
    fails. Implementations must instruct the repair model to preserve every
    field value verbatim and only correct structural problems.
    """

    async def repair(self, raw: str, schema_hint: str) -> str:
        """Return a JSON string matching ``schema_hint`` derived from ``raw``.

        Args:
            raw: The original (malformed) model output.
            schema_hint: A short natural-language description of the target
                JSON shape (keys + types). The repair model must preserve
                any values present in ``raw``.

        Returns:
            A string the caller should re-parse with the standard pipeline.
            Implementations should not raise on a repair-call failure;
            instead, return ``raw`` unchanged so callers fall through to
            the original error.
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
# Generic Toolsets
# =============================================================================


class SystemLimitsPort(Protocol):
    """Execution limits for the orchestration system loop."""

    max_depth: int
    max_children_per_node: int
    max_total_agents: int
    max_concurrent_workers: int
    max_concurrent_llm_calls: int
    llm_jitter_max_ms: int
    max_run_duration_seconds: float

    def is_workers_limited(self) -> bool: ...
    def is_llm_limited(self) -> bool: ...


class Toolset(Protocol):
    """Named tool provider that can advertise and execute tools."""

    @property
    def name(self) -> str: ...

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format."""
        ...

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Dispatch a tool call by name and return the result as a string."""
        ...


# =============================================================================
# Reconnaissance Tools (for manager/PENDING tool-calling)
# =============================================================================


class ReconToolPort(Toolset, Protocol):
    """Read-only reconnaissance tools for manager assessment/decomposition.

    These allow PENDING/MANAGER agents to inspect the codebase before
    deciding whether to execute or decompose. All operations are read-only.
    """

    async def read_file(
        self, path: str, max_lines: int = 200,
        start_line: int | None = None, end_line: int | None = None,
    ) -> str:
        """Read file contents. Use start_line/end_line for targeted reads."""
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

    async def get_symbols_overview(self, path: str) -> str:
        """Get function/class/method signatures without bodies."""
        ...

    async def read_symbol(self, path: str, symbol_name: str) -> str:
        """Read the body of a specific function/class by name."""
        ...
