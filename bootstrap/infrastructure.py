"""Infrastructure Layer - Adapter Factories.

This module provides factory functions for creating infrastructure adapters
that implement the ports defined by the domain layer.

Dependency: Infrastructure → Domain (ports, events, exceptions)

In Hexagonal Architecture, infrastructure adapters implement port interfaces
and use domain types (events, exceptions), creating a dependency on the domain layer.
This is correct - infrastructure is in the outer layer and depends on the inner domain.
"""

from dataclasses import dataclass
from typing import Literal

from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort
from infrastructure.adapters.claude_pty_adapter import ClaudeCodePTYAdapter
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.openhands_adapter import OpenHandsAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters.

    In production, these values should be loaded from environment variables
    or configuration files.
    """

    # PostgreSQL Event Store
    postgres_connection_string: str = "postgresql://arise:arise@localhost:5432/arise_events"

    # Worker Tool Configuration
    # Supported types: "claude_code" or "openhands"
    worker_tool_type: Literal["claude_code", "openhands"] = "openhands"
    worker_tool_model: str = "openai/gpt-4o"  # For openhands: LiteLLM model identifier
    worker_tool_timeout: int = 300  # seconds


@dataclass
class Infrastructure:
    """Container for all infrastructure adapters.

    This explicit structure makes it clear which adapters are available
    and enforces type safety at the bootstrap layer.
    """

    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort


def get_infrastructure(config: InfrastructureConfig | None = None) -> Infrastructure:
    """Create and return all infrastructure adapters.

    This factory function instantiates the concrete implementations of
    the ports defined by the domain layer. The adapters are "pluggable"
    and can be swapped without changing the domain or application logic.

    Args:
        config: Infrastructure configuration. Uses defaults if not provided.

    Returns:
        Infrastructure container with all adapters.

    Architecture Note:
        Infrastructure adapters implement the ports (interfaces) defined by
        the core domain. This inversion of dependencies is the essence of
        Hexagonal Architecture - the domain doesn't depend on infrastructure,
        infrastructure depends on domain interfaces.
    """
    if config is None:
        config = InfrastructureConfig()

    # PostgreSQL Event Store - implements EventStorePort
    event_store = PostgresEventStore(config.postgres_connection_string)

    # LiteLLM Adapter - implements LLMPort
    # Supports OpenAI, Anthropic, and 100+ LLM providers
    # No default model needed - agents specify models based on their roles
    llm_adapter = LiteLLMAdapter()

    # Worker Tool - implements WorkerToolPort
    # Select adapter based on configuration
    worker_tool: WorkerToolPort
    if config.worker_tool_type == "claude_code":
        # Claude Code PTY Adapter
        # Executes worker tasks using Claude Code CLI via pseudo-terminal
        # Requires: ANTHROPIC_API_KEY environment variable
        worker_tool = ClaudeCodePTYAdapter(timeout_seconds=config.worker_tool_timeout)
    else:
        # OpenHands SDK Adapter (default)
        # Executes worker tasks using OpenHands with any LLM provider
        # Requires: OPENAI_API_KEY or LLM_API_KEY environment variable
        worker_tool = OpenHandsAdapter(
            model=config.worker_tool_model,
            timeout_seconds=config.worker_tool_timeout,
        )

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
    )
