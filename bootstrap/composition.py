"""Composition helpers for bootstrap runtime wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from plugins.security import SecurityDomainPlugin
from presentation.cli import CLI, CLIConfig

from .application import ApplicationConfig, get_application
from .infrastructure import InfrastructureConfig, get_infrastructure


if TYPE_CHECKING:
    from collections.abc import Callable

    from config import Settings
    from core.application.execution_service import ProgressCallback
    from core.application.services import PromptStrategy
    from core.ports.domain_plugin_port import DomainPlugin


@dataclass(frozen=True, slots=True)
class DomainComponents:
    """Bundle the optional domain plugin and its paired prompt strategy."""

    plugin: DomainPlugin | None = None
    prompt_strategy: PromptStrategy | None = None


def _build_security_components(settings: Settings) -> DomainComponents:
    if not settings.security.enabled:
        return DomainComponents()

    plugin = SecurityDomainPlugin(enabled_tools=settings.security.tools)
    return DomainComponents(
        plugin=plugin,
        prompt_strategy=plugin.get_prompt_strategy(),
    )


_DOMAIN_COMPONENT_BUILDERS: dict[str, Callable[[Settings], DomainComponents]] = {
    "security": _build_security_components,
}

_DOMAIN_COMPONENT_ORDER: tuple[str, ...] = tuple(_DOMAIN_COMPONENT_BUILDERS)


def get_first_enabled_domain_components(settings: Settings) -> DomainComponents:
    """Return the first enabled domain plugin bundle, if any."""
    for name in _DOMAIN_COMPONENT_ORDER:
        components = _DOMAIN_COMPONENT_BUILDERS[name](settings)
        if components.plugin is not None:
            return components
    return DomainComponents()


def get_run_domain_components(
    settings: Settings,
    *,
    requested_domain: str | None,
    cve_file: object | None,
) -> DomainComponents:
    """Return the domain bundle for a run when explicitly requested."""
    if requested_domain is None and cve_file is None:
        return DomainComponents()

    domain_name = requested_domain or "security"
    builder = _DOMAIN_COMPONENT_BUILDERS.get(domain_name)
    return builder(settings) if builder is not None else DomainComponents()


def create_runtime_cli(
    settings: Settings,
    *,
    progress_callback: ProgressCallback | None = None,
    domain_components: DomainComponents | None = None,
) -> CLI:
    """Create a CLI with fully wired infrastructure and optional domain pieces."""
    active_domain_components = domain_components or DomainComponents()
    infra = get_infrastructure(
        InfrastructureConfig(
            postgres_connection_string=settings.database.connection_string,
            default_worker_tool=settings.worker.tool,
            worker_tool_model=settings.worker.model,
            worker_tool_timeout=settings.worker.timeout,
            worker_tool_max_iterations=settings.worker.max_iterations_per_run,
        )
    )

    plugin = active_domain_components.plugin
    if plugin is not None and hasattr(plugin, "set_container_runtime"):
        plugin.set_container_runtime(infra.secbench_runtime)

    app = get_application(
        infra,
        ApplicationConfig(
            topology=settings.orchestration.topology,
            concurrency=settings.orchestration.concurrency,
            tool_calling=settings.orchestration.tool_calling,
            max_retries=settings.orchestration.max_retries,
            poll_interval=settings.orchestration.poll_interval,
            max_run_duration_seconds=settings.orchestration.max_run_duration_seconds,
            max_redecompositions=settings.orchestration.max_redecompositions,
            skip_judge=settings.orchestration.skip_judge,
            boss_config=settings.boss,
            manager_config=settings.manager,
            output_directory=settings.output.directory,
            default_worker_tool=settings.worker.tool,
            domain_plugin=plugin,
            prompt_strategy=active_domain_components.prompt_strategy,
            progress_callback=progress_callback,
        ),
    )

    return CLI(
        execution_service=app.execution_service,
        config=CLIConfig(
            verbose=settings.output.verbose,
            output_directory=settings.output.directory,
            default_worker_tool=settings.worker.tool,
        ),
    )
