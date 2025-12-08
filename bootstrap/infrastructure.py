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
from infrastructure.adapters.composite_worker_adapter import CompositeWorkerAdapter
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.openhands_adapter import OpenHandsAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters.

    All fields are required and must be provided from config files
    (config/default.yaml, config/development.yaml, etc.) or environment
    variables. This ensures explicit configuration and prevents
    accidental use of hardcoded defaults.

    The bootstrap.py module is responsible for loading Settings from
    config files and mapping them to this config object.

    Raises:
        TypeError: If any required field is not provided.
    """

    # PostgreSQL Event Store
    postgres_connection_string: str

    # Worker Tool Configuration
    # Default tool used when agent config doesn't specify one
    default_worker_tool: str
    worker_tool_model: str  # For openhands: LiteLLM model identifier
    worker_tool_timeout: int  # seconds


@dataclass
class Infrastructure:
    """Container for all infrastructure adapters.

    This explicit structure makes it clear which adapters are available
    and enforces type safety at the bootstrap layer.
    """

    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort


def get_infrastructure(config: InfrastructureConfig) -> Infrastructure:
    """Create and return all infrastructure adapters.

    This factory function instantiates the concrete implementations of
    the ports defined by the domain layer. The adapters are "pluggable"
    and can be swapped without changing the domain or application logic.

    Args:
        config: Infrastructure configuration. Must be explicitly provided
                from config files or environment variables. No defaults.

    Returns:
        Infrastructure container with all adapters.

    Raises:
        TypeError: If config is None or missing required fields.

    Architecture Note:
        Infrastructure adapters implement the ports (interfaces) defined by
        the core domain. This inversion of dependencies is the essence of
        Hexagonal Architecture - the domain doesn't depend on infrastructure,
        infrastructure depends on domain interfaces.
    """

    # PostgreSQL Event Store - implements EventStorePort
    event_store = PostgresEventStore(config.postgres_connection_string)

    # LiteLLM Adapter - implements LLMPort
    # Supports OpenAI, Anthropic, and 100+ LLM providers
    # No default model needed - agents specify models based on their roles
    llm_adapter = LiteLLMAdapter()

    # Worker Tool - implements WorkerToolPort
    # Use composite adapter that routes to the correct tool based on agent config
    # This allows parent agents to specify which tool their children should use

    # Create individual adapters
    claude_code_adapter = ClaudeCodePTYAdapter(timeout_seconds=config.worker_tool_timeout)
    openhands_adapter = OpenHandsAdapter(
        model=config.worker_tool_model,
        timeout_seconds=config.worker_tool_timeout,
    )

    # Create composite adapter that routes based on tool_name in task context
    worker_tool: WorkerToolPort = CompositeWorkerAdapter(
        adapters={
            "claude_code": claude_code_adapter,
            "openhands": openhands_adapter,
        },
        default_tool=config.default_worker_tool,
    )

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
    )
