"""Infrastructure Layer - Adapter Factories."""

from dataclasses import dataclass
from typing import Literal

import infrastructure.adapters.sinks as _sinks  # noqa: F401 - registers sinks via decorators
from core.ports.event_store_port import EventStorePort
from core.ports.runtime_ports import (
    FormatRepairerPort,
    LLMPort,
    ReconToolPort,
    SharedContextPort,
    WorkerToolPort,
)
from infrastructure.adapters.litellm_adapter import LiteLLMAdapter
from infrastructure.adapters.llm_format_repairer import LLMFormatRepairer
from infrastructure.adapters.openrouter_adapter import OpenRouterAdapter
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from infrastructure.adapters.recon_tool_adapter import ReconToolAdapter
from infrastructure.adapters.shared_context_adapter import PostgresSharedContextAdapter
from infrastructure.adapters.worker import (
    ADKAdapterConfig,
    ClaudeAgentSDKAdapter,
    GoogleADKAdapter,
    OpenHandsAdapter,
    SDKAdapterConfig,
)
from infrastructure.adapters.worker.shared_code_context import SharedCodeContextProvider


type WorkerToolType = Literal["claude_code", "openhands", "google_adk"]


def _build_llm_adapter() -> LLMPort:
    """Pick the LLM gateway based on ``ARISE_LLM_GATEWAY`` env var.

    Values:
        - ``litellm`` (default) — multi-provider routing via LiteLLM
        - ``openrouter`` — single gateway via the official ``openai`` SDK
          pointed at https://openrouter.ai. Requires ``OPENROUTER_API_KEY``.

    Keeping both behind a switch lets us A/B test 429 behavior and roll back
    quickly if a model isn't available through OpenRouter.
    """
    import os

    gateway = os.environ.get("ARISE_LLM_GATEWAY", "litellm").lower()
    if gateway == "openrouter":
        return OpenRouterAdapter(app_title="arise-sec-lion")
    if gateway == "litellm":
        return LiteLLMAdapter()
    raise ValueError(
        f"Unknown ARISE_LLM_GATEWAY={gateway!r}; expected 'litellm' or 'openrouter'"
    )


@dataclass
class InfrastructureConfig:
    """Configuration for infrastructure adapters."""

    postgres_connection_string: str
    default_worker_tool: WorkerToolType
    worker_tool_model: str
    worker_tool_timeout: int
    worker_allowed_tools: list[str] | None = None
    worker_disallowed_tools: list[str] | None = None
    worker_mcp_tools: list[str] | None = None
    worker_tool_max_iterations: int = 20
    worker_tool_base_url: str | None = None
    worker_shared_session: bool = False
    format_repairer_enabled: bool = False
    format_repairer_model: str | None = None
    format_repairer_max_tokens: int = 16000
    format_repairer_api_base: str | None = None
    format_repairer_max_concurrent: int = 3
    # asyncpg pool sizing. Defaults match the adapter's own defaults so
    # unchanged deployments behave identically; raise ``pool_max`` to
    # widen the connection pool under load.
    pool_min: int = 10
    pool_max: int = 10


@dataclass
class Infrastructure:
    """Container for all infrastructure adapters."""

    event_store: EventStorePort
    llm_adapter: LLMPort
    worker_tool: WorkerToolPort
    shared_context: SharedContextPort
    recon_tool: ReconToolPort
    format_repairer: FormatRepairerPort | None
    # Shared code-prefix provider; None unless the shared-context flag is on.
    # The same instance backs worker-side capture (in the adapter) and prompt-side
    # rebuild (injected into the orchestrator) so both see one render memo.
    shared_code_context: SharedCodeContextProvider | None = None


def _create_worker_adapter(
    config: InfrastructureConfig,
    shared_code_context: SharedCodeContextProvider | None,
) -> WorkerToolPort:
    """Create the configured worker adapter.

    Direct adapter selection based on config - no composite wrapper. The shared
    code provider is wired only into the OpenHands adapter (the worker SDK whose
    file-editor observations carry the viewed content to capture).
    """
    if config.default_worker_tool == "claude_code":
        return ClaudeAgentSDKAdapter(
            SDKAdapterConfig(
                timeout_seconds=config.worker_tool_timeout,
                model=config.worker_tool_model,
                allowed_tools=config.worker_allowed_tools or SDKAdapterConfig().allowed_tools,
                disallowed_tools=config.worker_disallowed_tools or [],
            )
        )
    if config.default_worker_tool == "openhands":
        return OpenHandsAdapter(
            model=config.worker_tool_model,
            timeout_seconds=config.worker_tool_timeout,
            max_iterations_per_run=config.worker_tool_max_iterations,
            base_url=config.worker_tool_base_url,
            allowed_tools=config.worker_allowed_tools,
            mcp_tools=config.worker_mcp_tools,
            shared_code_port=shared_code_context,
        )
    if config.default_worker_tool == "google_adk":
        return GoogleADKAdapter(
            ADKAdapterConfig(
                model=config.worker_tool_model,
                timeout_seconds=config.worker_tool_timeout,
                allowed_tools=(
                    config.worker_allowed_tools
                    or ADKAdapterConfig(model=config.worker_tool_model).allowed_tools
                ),
                disallowed_tools=config.worker_disallowed_tools or [],
            )
        )
    raise ValueError(f"Unknown worker tool: {config.default_worker_tool}")


def get_infrastructure(config: InfrastructureConfig) -> Infrastructure:
    event_store = PostgresEventStore(
        config.postgres_connection_string,
        pool_min=config.pool_min,
        pool_max=config.pool_max,
    )
    llm_adapter = _build_llm_adapter()
    # Gate the shared code-prefix optimization on the umbrella flag. Off => no
    # provider, so workers neither capture nor inject (byte-identical to today).
    shared_code_context = (
        SharedCodeContextProvider(event_store) if config.worker_shared_session else None
    )
    worker_tool = _create_worker_adapter(config, shared_code_context)
    shared_context = PostgresSharedContextAdapter(event_store)
    recon_tool = ReconToolAdapter()

    format_repairer: FormatRepairerPort | None = None
    if config.format_repairer_enabled:
        if not config.format_repairer_model:
            raise ValueError("format_repairer_model is required when format repairer is enabled")
        format_repairer = LLMFormatRepairer(
            llm_port=llm_adapter,
            model=config.format_repairer_model,
            max_tokens=config.format_repairer_max_tokens,
            api_base=config.format_repairer_api_base,
            max_concurrent=config.format_repairer_max_concurrent,
        )

    return Infrastructure(
        event_store=event_store,
        llm_adapter=llm_adapter,
        worker_tool=worker_tool,
        shared_context=shared_context,
        recon_tool=recon_tool,
        format_repairer=format_repairer,
        shared_code_context=shared_code_context,
    )
