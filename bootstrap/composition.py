"""Composition helpers for bootstrap runtime wiring."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from core.application.execution_service import FlatModeBundle
from core.application.run_invariants import (
    TaskPromptSpec,
    ToolPolicy,
    build_timeouts,
    build_workspace_spec,
)
from infrastructure.workers import ClaudeCodeWorker
from plugins.security import CVEInstance, SecurityDomainPlugin
from plugins.security.docker_runtime import DockerSecBenchRuntime
from presentation.cli import CLI, CLIConfig

from .application import ApplicationConfig, get_application
from .infrastructure import InfrastructureConfig, get_infrastructure


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from config import Settings
    from core.application.execution_service import (
        FlatInvariantBuilder,
        ProgressCallback,
    )
    from core.application.services import PromptStrategy
    from core.ports.domain_plugin_port import DomainPlugin
    from core.ports.worker_port import WorkerPort


@dataclass(frozen=True, slots=True)
class DomainComponents:
    """Bundle the optional domain plugin and its paired prompt strategy."""

    plugin: DomainPlugin | None = None
    prompt_strategy: PromptStrategy | None = None
    domain_key: str | None = None


def _build_security_components(settings: Settings) -> DomainComponents:
    if not settings.security.enabled:
        return DomainComponents()

    runtime = DockerSecBenchRuntime()
    plugin = SecurityDomainPlugin(enabled_tools=settings.security.tools)
    plugin.set_container_runtime(runtime)
    return DomainComponents(
        plugin=plugin,
        prompt_strategy=plugin.get_prompt_strategy(),
        domain_key="secbench",
    )


_DOMAIN_COMPONENT_BUILDERS: dict[str, Callable[[Settings], DomainComponents]] = {
    "security": _build_security_components,
}

_DOMAIN_COMPONENT_ORDER: tuple[str, ...] = tuple(_DOMAIN_COMPONENT_BUILDERS)


def build_domain_plugin(settings: Settings) -> DomainPlugin | None:
    """Return the first enabled domain plugin, or None.

    Intended for query API bootstrap where only the plugin (not full
    DomainComponents) is needed.
    """
    return get_first_enabled_domain_components(settings).plugin


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
    context_file: object | None,
) -> DomainComponents:
    """Return the domain bundle for a run when explicitly requested."""
    if requested_domain is None and context_file is None:
        return DomainComponents()

    domain_name = requested_domain or "security"
    builder = _DOMAIN_COMPONENT_BUILDERS.get(domain_name)
    return builder(settings) if builder is not None else DomainComponents()


def _build_flat_worker(settings: Settings) -> WorkerPort:
    """Construct the ``WorkerPort`` adapter used by flat-mode dispatch.

    Scope: A-cells (A1, A2) only use ``worker.tool='claude_code'`` — flat mode
    is currently a single-runner contract. Other backends (openhands, google_adk)
    raise ``NotImplementedError`` so a misconfigured cell fails loudly at
    composition time. Add branches here when their A-cell configs land.
    """
    tool = settings.worker.tool
    if tool == "claude_code":
        params = settings.worker.tool_params.claude_code
        if params is None:
            raise RuntimeError(
                "worker.tool_params.claude_code must be populated when "
                "worker.tool='claude_code'"
            )
        return ClaudeCodeWorker(
            model=settings.worker.model,
            output_format=params.output_format,
            include_partial_messages=params.include_partial_messages,
            max_turns=params.max_turns,
            use_global_config=params.use_global_config,
        )
    raise NotImplementedError(
        f"flat mode currently supports only worker.tool='claude_code'; got {tool!r}. "
        "openhands and google_adk flat-mode adapters are out of scope until their "
        "A-cell configs land."
    )


def _make_flat_invariant_builder(
    *,
    settings: Settings,
    context_file: Path | None,
) -> FlatInvariantBuilder:
    """Return a closure that produces a ``FlatModeBundle`` per flat-mode run.

    The closure validates that ``domain_context`` is a ``CVEInstance``, then
    assembles the four ``run_invariants`` value objects from ``Settings`` and
    the run-scoped inputs. ``context_file`` is captured for BUG-A2 — the future
    ``extend_flat_prompt`` renderer will read CVE metadata from this JSON path
    when the inferred ``domain_context`` is sparse. The current closure does
    not consume it.
    """
    del context_file  # Reserved for BUG-A2; remove the ``del`` when consumed.

    def _builder(
        *,
        task: str,
        domain_context: object | None,
        run_dir: Path,
    ) -> FlatModeBundle:
        if not isinstance(domain_context, CVEInstance):
            raise ValueError(
                "flat mode requires a CVEInstance domain_context; got "
                f"{type(domain_context).__name__}"
            )

        # A-cell tool policy is expressed at the top level in YAML — A2 sets
        # ``worker.disallowed_tools: ["Task"]`` to suppress Claude's Task
        # subagent tool, the entire purpose of the A1-vs-A2 contrast. Read
        # from there directly; ``tool_params.claude_code.{allowed,disallowed}_tools``
        # are the per-runner defaults and are NOT the source of truth for the
        # flat-mode policy contract.
        tool_policy = ToolPolicy(
            allowed=tuple(settings.worker.allowed_tools),
            disallowed=tuple(settings.worker.disallowed_tools),
            allowed_bash_commands=(
                tuple(settings.security.tools) if settings.security.enabled else ()
            ),
        )

        # TODO(BUG-A2): replace placeholder with extend_flat_prompt output.
        rendered_prompt = f"<flat-mode placeholder for CVE {domain_context.instance_id}>"
        spec = TaskPromptSpec(
            rendered_prompt=rendered_prompt,
            prompt_sha=sha256(rendered_prompt.encode("utf-8")).hexdigest(),
            cve_context=domain_context.to_template_context(),
            task=task,
        )
        return FlatModeBundle(
            spec=spec,
            tool_policy=tool_policy,
            timeouts=build_timeouts(settings),
            workspace=build_workspace_spec(run_dir=run_dir, extras={}),
        )

    return _builder


def create_runtime_cli(
    settings: Settings,
    *,
    progress_callback: ProgressCallback | None = None,
    domain_components: DomainComponents | None = None,
    context_file: Path | None = None,
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
            worker_tool_base_url=settings.worker.base_url,
            format_repairer_enabled=settings.format_repairer.enabled,
            format_repairer_model=settings.format_repairer.model,
            format_repairer_max_tokens=settings.format_repairer.max_tokens,
            format_repairer_api_base=settings.format_repairer.api_base,
        )
    )

    plugin = active_domain_components.plugin

    is_flat = settings.orchestration.mode == "flat"
    flat_worker = _build_flat_worker(settings) if is_flat else None
    flat_invariant_builder = (
        _make_flat_invariant_builder(settings=settings, context_file=context_file)
        if is_flat
        else None
    )

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
            domain_key=active_domain_components.domain_key,
            mode=settings.orchestration.mode,
            flat_worker=flat_worker,
            flat_invariant_builder=flat_invariant_builder,
        ),
    )

    return CLI(
        execution_service=app.execution_service,
        event_store=infra.event_store,
        config=CLIConfig(
            verbose=settings.output.verbose,
            output_directory=settings.output.directory,
            default_worker_tool=settings.worker.tool,
        ),
    )
