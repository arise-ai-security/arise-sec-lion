"""Runtime ports: protocols for LLM, worker, cost, context, and streaming.

Small port protocols consolidated into one file. EventStorePort stays
in its own file due to size and ISP split.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.events.events import DomainEvent
from core.domain.values.llm_response import LLMResponse

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
