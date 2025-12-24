"""Infrastructure Layer - Adapter Factories."""

from dataclasses import dataclass, field

from core.ports.context_dashboard_port import ContextDashboardPort
from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.worker_port import WorkerToolPort
from infrastructure.adapters.claude_pty_adapter import ClaudeCodePTYAdapter
from infrastructure.adapters.composite_worker_adapter import CompositeWorkerAdapter
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.openhands_adapter import OpenHandsAdapter
from infrastructure.adapters.postgres_context_dashboard import PostgresContextDashboard
from infrastructure.adapters.postgres_event_store import PostgresEventStore


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters."""

    postgres_connection_string: str
    default_worker_tool: str
    worker_tool_model: str
    worker_tool_timeout: int
    context_dashboard_enabled: bool = field(default=True)


@dataclass
class Infrastructure:
    """Container for all infrastructure adapters."""

    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort
    context_dashboard: ContextDashboardPort | None = field(default=None)


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

    # Context dashboard for cross-session learning
    context_dashboard: ContextDashboardPort | None = None
    if config.context_dashboard_enabled:
        context_dashboard = PostgresContextDashboard(config.postgres_connection_string)

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
        context_dashboard=context_dashboard,
    )
