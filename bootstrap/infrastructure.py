"""Infrastructure Layer - Adapter Factories."""

from dataclasses import dataclass
from typing import Literal

from core.ports.event_store_port import EventStorePort
from core.ports.llm_port import LLMPort
from core.ports.repository_setup_port import RepositorySetupPort
from core.ports.research_port import ResearchPort
from core.ports.shared_context_port import SharedContextPort
from core.ports.worker_port import WorkerToolPort
from core.ports.workspace_scanner_port import WorkspaceScannerPort
from infrastructure.adapters.git_repository_adapter import GitRepositoryAdapter
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from infrastructure.adapters.research import LiteLLMResearchAdapter
from infrastructure.adapters.shared_context_adapter import PostgresSharedContextAdapter
from infrastructure.adapters.workspace_scanner import FileSystemWorkspaceScanner
from infrastructure.adapters.worker import (
    ADKAdapterConfig,
    ClaudeAgentSDKAdapter,
    GoogleADKAdapter,
    OpenHandsAdapter,
    SDKAdapterConfig,
)


type WorkerToolType = Literal["claude_code", "openhands", "google_adk"]


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters."""

    postgres_connection_string: str
    default_worker_tool: WorkerToolType
    worker_tool_model: str
    worker_tool_timeout: int


@dataclass
class Infrastructure:
    """Container for all infrastructure adapters."""

    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort
    shared_context: SharedContextPort
    research_adapter: ResearchPort
    repository_setup: RepositorySetupPort
    workspace_scanner: WorkspaceScannerPort


def _create_worker_adapter(config: InfrastructureConfig) -> WorkerToolPort:
    """Create the configured worker adapter.

    Direct adapter selection based on config - no composite wrapper.
    """
    if config.default_worker_tool == "claude_code":
        return ClaudeAgentSDKAdapter(
            SDKAdapterConfig(
                timeout_seconds=config.worker_tool_timeout,
                model=config.worker_tool_model,
            )
        )
    elif config.default_worker_tool == "openhands":
        return OpenHandsAdapter(
            model=config.worker_tool_model,
            timeout_seconds=config.worker_tool_timeout,
        )
    elif config.default_worker_tool == "google_adk":
        return GoogleADKAdapter(
            ADKAdapterConfig(
                model=config.worker_tool_model,
                timeout_seconds=config.worker_tool_timeout,
            )
        )
    else:
        raise ValueError(f"Unknown worker tool: {config.default_worker_tool}")


def get_infrastructure(config: InfrastructureConfig) -> Infrastructure:
    """Create all infrastructure adapters."""
    event_store = PostgresEventStore(config.postgres_connection_string)
    llm_adapter = LiteLLMAdapter()
    worker_tool = _create_worker_adapter(config)
    shared_context = PostgresSharedContextAdapter(event_store)
    research_adapter = LiteLLMResearchAdapter()
    repository_setup = GitRepositoryAdapter()
    workspace_scanner = FileSystemWorkspaceScanner()

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
        shared_context=shared_context,
        research_adapter=research_adapter,
        repository_setup=repository_setup,
        workspace_scanner=workspace_scanner,
    )
