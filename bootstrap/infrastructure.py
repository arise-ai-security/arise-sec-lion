"""Infrastructure Layer - Adapter Factories."""

from dataclasses import dataclass

from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.shared_context_port import SharedContextPort
from core.ports.worker_port import WorkerToolPort
from infrastructure.adapters.claude_pty_adapter import ClaudeCodePTYAdapter
from infrastructure.adapters.composite_worker_adapter import CompositeWorkerAdapter
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.openhands_adapter import OpenHandsAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from infrastructure.adapters.shared_context_adapter import PostgresSharedContextAdapter


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters."""

    postgres_connection_string: str
    default_worker_tool: str
    worker_tool_model: str
    worker_tool_timeout: int


@dataclass
class Infrastructure:
    """Container for all infrastructure adapters."""

    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort
    shared_context: SharedContextPort


def get_infrastructure(config: InfrastructureConfig) -> Infrastructure:
    """Create all infrastructure adapters."""
    event_store = PostgresEventStore(config.postgres_connection_string)
    llm_adapter = LiteLLMAdapter()

    claude_code_adapter = ClaudeCodePTYAdapter(timeout_seconds=config.worker_tool_timeout)
    openhands_adapter = OpenHandsAdapter(
        model=config.worker_tool_model,
        timeout_seconds=config.worker_tool_timeout,
    )

    worker_tool: WorkerToolPort = CompositeWorkerAdapter(
        adapters={
            "claude_code": claude_code_adapter,
            "openhands": openhands_adapter,
        },
        default_tool=config.default_worker_tool,
    )

    # Shared context uses the same event store
    shared_context = PostgresSharedContextAdapter(event_store)

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
        shared_context=shared_context,
    )
