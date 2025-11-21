"""Infrastructure Layer - Adapter Factories.

This module provides factory functions for creating infrastructure adapters
that implement the ports defined by the domain layer.

Dependency: Infrastructure → Domain (ports, events, exceptions)

In Hexagonal Architecture, infrastructure adapters implement port interfaces
and use domain types (events, exceptions), creating a dependency on the domain layer.
This is correct - infrastructure is in the outer layer and depends on the inner domain.
"""

from dataclasses import dataclass

from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort
from infrastructure.adapters.claude_pty_adapter import ClaudeCodePTYAdapter
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters.

    In production, these values should be loaded from environment variables
    or configuration files.
    """

    # PostgreSQL Event Store
    postgres_connection_string: str = "postgresql://arise:arise@localhost:5432/arise_events"

    # LiteLLM Configuration
    llm_model: str = "gpt-4o-mini"

    # Worker Tool Configuration
    # (ClaudeCodePTYAdapter doesn't need config for now)


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
    # Pass default_config dict with model
    llm_adapter = LiteLLMAdapter(default_config={"model": config.llm_model})

    # Claude Code PTY Adapter - implements WorkerToolPort
    # Executes worker tasks using Claude Code CLI via pseudo-terminal
    worker_tool = ClaudeCodePTYAdapter()

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
    )
