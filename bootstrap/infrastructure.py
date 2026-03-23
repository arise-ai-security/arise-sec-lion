"""Infrastructure Layer - Adapter Factories."""

from dataclasses import dataclass
from typing import Literal

import infrastructure.adapters.sinks as _sinks  # noqa: F401 - registers sinks via decorators
from core.ports.event_store_port import EventStorePort
from core.ports.runtime_ports import LLMPort, ReconToolPort, SharedContextPort, WorkerToolPort
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter
from infrastructure.adapters.secbench_runtime import DockerSecBenchRuntime
from infrastructure.adapters.shared_context_adapter import PostgresSharedContextAdapter
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
    recon_tool: ReconToolPort
    secbench_runtime: DockerSecBenchRuntime


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
    if config.default_worker_tool == "openhands":
        return OpenHandsAdapter(
            model=config.worker_tool_model,
            timeout_seconds=config.worker_tool_timeout,
        )
    if config.default_worker_tool == "google_adk":
        return GoogleADKAdapter(
            ADKAdapterConfig(
                model=config.worker_tool_model,
                timeout_seconds=config.worker_tool_timeout,
            )
        )
    raise ValueError(f"Unknown worker tool: {config.default_worker_tool}")


def get_infrastructure(config: InfrastructureConfig) -> Infrastructure:
    """Create all infrastructure adapters."""
    event_store = PostgresEventStore(config.postgres_connection_string)
    llm_adapter = LiteLLMAdapter()
    worker_tool = _create_worker_adapter(config)
    shared_context = PostgresSharedContextAdapter(event_store)
    recon_tool = ReconToolAdapter()
    secbench_runtime = DockerSecBenchRuntime()

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
        shared_context=shared_context,
        recon_tool=recon_tool,
        secbench_runtime=secbench_runtime,
    )
