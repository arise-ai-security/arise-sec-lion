"""Composition helpers for bootstrap runtime wiring."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from core.application.execution_service import FlatModeBundle
from core.application.run_invariants import (
    TaskPromptSpec,
    ToolPolicy,
    build_timeouts,
    build_workspace_spec,
)
from core.application.services import PromptBuilder
from infrastructure.adapters.worker import OpenHandsAdapter
from infrastructure.cleanup.docker_pid import docker_pid_cleanup
from infrastructure.workers import ClaudeCodeWorker, OpenHandsWorker
from plugins.security import CVEInstance, SecurityDomainPlugin
from plugins.security.config import SecurityPluginConfig
from plugins.security.docker_runtime import DockerSecBenchRuntime
from presentation.cli import CLI, CLIConfig
from presentation.persistence.invocation_hash import compute_config_sha256

from .application import ApplicationConfig, get_application
from .infrastructure import InfrastructureConfig, get_infrastructure


logger = logging.getLogger(__name__)


def _effective_config_hash(settings: Settings) -> str:
    """Hash the redacted effective settings, including opaque run provenance."""
    return compute_config_sha256(settings)


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from config import ApiSettings, Settings, ToolCallingConfig
    from core.application.execution_service import (
        FlatInvariantBuilder,
        ProgressCallback,
    )
    from core.application.services import PromptStrategy
    from core.ports.domain_plugin_port import DomainPlugin
    from core.ports.worker_port import WorkerPort
    from infrastructure.cleanup.registry import CleanupRegistry


@dataclass(frozen=True, slots=True)
class DomainComponents:
    """Bundle the optional domain plugin and its paired prompt strategy."""

    plugin: DomainPlugin | None = None
    prompt_strategy: PromptStrategy | None = None
    domain_key: str | None = None
    tool_policy: dict[str, object] | None = None
    artifact_subdirectory: str | None = None


def _security_config(settings: Settings | ApiSettings) -> SecurityPluginConfig:
    options = settings.domain_plugins
    raw = options.get("security", {}) if isinstance(options, dict) else {}
    return SecurityPluginConfig.model_validate(raw)


def _build_security_components(settings: Settings | ApiSettings) -> DomainComponents:
    security = _security_config(settings)
    if not security.enabled:
        return DomainComponents()

    runtime = DockerSecBenchRuntime(
        network_mode=security.worker_network_mode,
        timeout_seconds=security.worker_docker_timeout_seconds,
        tools_image_registry=security.tools_image_registry,
    )
    plugin = SecurityDomainPlugin(
        enabled_tools=security.tools,
        shared_code_prefix_first=settings.orchestration.shared_code_prefix_first,
        route_policy_version=security.route_policy_version,
    )
    plugin.set_container_runtime(runtime)
    return DomainComponents(
        plugin=plugin,
        prompt_strategy=plugin.get_prompt_strategy(),
        domain_key="secbench",
        tool_policy=security.tool_policy,
        artifact_subdirectory="testcase",
    )


_DOMAIN_COMPONENT_BUILDERS: dict[str, Callable[[Settings | ApiSettings], DomainComponents]] = {
    "security": _build_security_components,
}

_DOMAIN_COMPONENT_ORDER: tuple[str, ...] = tuple(_DOMAIN_COMPONENT_BUILDERS)


def available_domain_names() -> tuple[str, ...]:
    """Return domain names registered at the composition root."""
    return _DOMAIN_COMPONENT_ORDER


def _tool_calling_config(
    settings: Settings, components: DomainComponents
) -> ToolCallingConfig:
    tool_calling = settings.orchestration.tool_calling
    if components.domain_key is None or components.tool_policy is None:
        return tool_calling
    policies = tool_calling.policies.to_raw_dict()
    policies.setdefault("domains", {})[components.domain_key] = components.tool_policy
    return tool_calling.model_copy(
        update={"policies": type(tool_calling.policies).model_validate(policies)}
    )


def build_domain_plugin(settings: Settings | ApiSettings) -> DomainPlugin | None:
    """Return the first enabled domain plugin, or None.

    Intended for query API bootstrap where only the plugin (not full
    DomainComponents) is needed.
    """
    return get_first_enabled_domain_components(settings).plugin


def get_first_enabled_domain_components(settings: Settings | ApiSettings) -> DomainComponents:
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
    if requested_domain is None and context_file is None:
        return DomainComponents()

    domain_name = requested_domain or "security"
    builder = _DOMAIN_COMPONENT_BUILDERS.get(domain_name)
    return builder(settings) if builder is not None else DomainComponents()


def _build_flat_worker(settings: Settings) -> WorkerPort:
    """Construct the ``WorkerPort`` adapter used by flat-mode dispatch.

    ``claude_code`` and ``openhands`` are wired for flat mode at composition
    time. ``google_adk`` raises ``NotImplementedError`` so a misconfigured
    run fails loudly at composition time. Add branches here when additional
    flat-mode workers land.
    """
    tool = settings.worker.tool
    if tool == "claude_code":
        params = settings.worker.tool_params.claude_code
        if params is None:
            raise RuntimeError(
                "worker.tool_params.claude_code must be populated when worker.tool='claude_code'"
            )
        return ClaudeCodeWorker(
            model=settings.worker.model,
            output_format=params.output_format,
            include_partial_messages=params.include_partial_messages,
            max_turns=params.max_turns,
            use_global_config=params.use_global_config,
        )
    if tool == "openhands":
        oh_params = settings.worker.tool_params.openhands
        if oh_params is None:
            raise RuntimeError(
                "worker.tool_params.openhands must be populated when worker.tool='openhands'"
            )
        adapter = OpenHandsAdapter(
            model=settings.worker.model,
            timeout_seconds=settings.worker.timeout,
            max_iterations_per_run=settings.worker.max_iterations_per_run,
            base_url=settings.worker.base_url,
            allowed_tools=settings.worker.allowed_tools,
            mcp_tools=oh_params.mcp_tools,
            mcp_tool_timeout_seconds=oh_params.mcp_tool_timeout_seconds,
            enable_subagents=oh_params.enable_subagents,
        )
        return OpenHandsWorker(adapter=adapter)
    raise NotImplementedError(
        f"flat mode supports worker.tool in {{'claude_code', 'openhands'}}; got {tool!r}. "
        "The google_adk flat-mode adapter is not yet wired."
    )


def _flat_subagent_enabled(settings: Settings) -> bool:
    """Whether the flat worker has a subagent-delegation tool available.

    ``claude_code`` exposes Claude's ``Task`` tool unless explicitly
    disallowed (assumes ``allowed_tools: ["*"]``; an explicit allowlist
    excluding ``Task`` would still get the note — revisit the predicate when
    introducing such a config). ``openhands`` gates its native
    task-delegation tool behind ``tool_params.openhands.enable_subagents``,
    which is also what arms the tool in the adapter, so note and capability
    stay in lockstep.
    """
    if settings.worker.tool == "openhands":
        params = settings.worker.tool_params.openhands
        return params is not None and params.enable_subagents
    return "Task" not in settings.worker.disallowed_tools


def _make_flat_invariant_builder(
    *,
    settings: Settings,
    prompt_builder: PromptBuilder,
    context_file: Path | None = None,
) -> FlatInvariantBuilder:
    """Return a closure that produces a ``FlatModeBundle`` per flat-mode run.

    The closure validates that ``domain_context`` is a ``CVEInstance``, then
    assembles the four ``run_invariants`` value objects from ``Settings`` and
    the run-scoped inputs. The rendered prompt is composed via
    ``prompt_builder.build_flat_prompt`` so the same ``PromptBuilder``
    instance is shared with ``ApplicationConfig`` (and hence the live
    ``AgentExecutionService``).

    ``context_file`` is reserved for a future refinement that lets the
    closure ingest CVE metadata from a JSON path when the inferred
    ``domain_context`` is sparse; the current closure does not consume it.
    """
    del context_file  # Reserved; remove the ``del`` when consumed.

    # ``subagent_enabled`` is derived once at factory time — settings are
    # constant for the lifetime of this closure. The flat prompt appends
    # ``FLAT_SUBAGENT_NOTE`` only when the worker can actually delegate;
    # see ``_flat_subagent_enabled`` for the per-tool predicate.
    subagent_enabled = _flat_subagent_enabled(settings)

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

        # Flat-mode tool policy is expressed at the top level in YAML; for
        # example, some configs set ``worker.disallowed_tools: ["Task"]`` to
        # suppress Claude's Task subagent tool. Read from there directly;
        # ``tool_params.claude_code.{allowed,disallowed}_tools`` are the
        # per-runner defaults and are NOT the source of truth for the
        # flat-mode policy contract.
        tool_policy = ToolPolicy(
            allowed=tuple(settings.worker.allowed_tools),
            disallowed=tuple(settings.worker.disallowed_tools),
            allowed_bash_commands=tuple(_security_config(settings).tools),
        )

        rendered_prompt = prompt_builder.build_flat_prompt(
            task_description=task,
            agent_id=uuid4(),
            domain_context=domain_context,
            subagent_enabled=subagent_enabled,
        )
        spec = TaskPromptSpec(
            rendered_prompt=rendered_prompt,
            prompt_sha=sha256(rendered_prompt.encode("utf-8")).hexdigest(),
            domain_context=domain_context.to_template_context(),
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
    cleanup_registry: CleanupRegistry | None = None,
) -> CLI:
    active_domain_components = domain_components or DomainComponents()

    # Register the Docker PID-label cleanup handler when the security plugin
    # is active. Other plugins (and the query-only paths that pass
    # ``cleanup_registry=None``) do not produce Docker workload, so the
    # registration is conditional. The handler itself is best-effort and
    # idempotent against an empty container set.
    if cleanup_registry is not None and isinstance(
        active_domain_components.plugin, SecurityDomainPlugin
    ):
        cleanup_registry.register("docker-by-pid", docker_pid_cleanup)

    openhands_params = settings.worker.tool_params.openhands
    worker_mcp_tools = (
        list(openhands_params.mcp_tools)
        if settings.worker.tool == "openhands" and openhands_params is not None
        else None
    )
    infra = get_infrastructure(
        InfrastructureConfig(
            postgres_connection_string=settings.database.connection_string,
            pool_min=settings.database.pool_min,
            pool_max=settings.database.pool_max,
            default_worker_tool=settings.worker.tool,
            worker_tool_model=settings.worker.model,
            worker_model_overrides=dict(settings.worker.model_overrides),
            worker_tool_timeout=settings.worker.timeout,
            worker_allowed_tools=list(settings.worker.allowed_tools),
            worker_disallowed_tools=list(settings.worker.disallowed_tools),
            worker_mcp_tools=worker_mcp_tools,
            worker_mcp_tool_timeout_seconds=(
                openhands_params.mcp_tool_timeout_seconds
                if settings.worker.tool == "openhands" and openhands_params is not None
                else 600
            ),
            worker_tool_max_iterations=settings.worker.max_iterations_per_run,
            worker_tool_base_url=settings.worker.base_url,
            worker_run_scoped_cache_key=(
                settings.worker.tool == "openhands"
                and openhands_params is not None
                and openhands_params.run_scoped_prompt_cache_key
            ),
            worker_reasoning_effort=settings.worker.reasoning_effort,
            worker_reasoning_effort_overrides=dict(settings.worker.reasoning_effort_overrides),
            worker_shared_session=settings.orchestration.shared_worker_session,
            shared_code_skip_dir_listings=settings.orchestration.shared_code_skip_dir_listings,
            shared_code_render_mode=settings.orchestration.shared_code_render_mode,
            shared_code_index_enabled=settings.orchestration.shared_code_index,
            scoped_worker_context=settings.orchestration.scoped_worker_context,
            source_context_token_budget=settings.orchestration.source_context_token_budget,
            metadata_context_token_budget=settings.orchestration.metadata_context_token_budget,
            format_repairer_enabled=settings.format_repairer.enabled,
            format_repairer_model=settings.format_repairer.model,
            format_repairer_max_tokens=settings.format_repairer.max_tokens,
            format_repairer_api_base=settings.format_repairer.api_base,
            format_repairer_max_concurrent=settings.format_repairer.max_concurrent,
        )
    )

    plugin = active_domain_components.plugin

    # Construct PromptBuilder once and share it between the flat-mode closure
    # (which needs it to render the flat prompt at run time) and the
    # AgentExecutionService (which needs it for hierarchical mode). Keeping
    # one instance avoids divergent Jinja envs and double initialization.
    prompt_builder = PromptBuilder(
        "prompts",
        settings.worker.tool,
        strategy=active_domain_components.prompt_strategy,
    )

    is_flat = settings.orchestration.mode == "flat"
    flat_worker = _build_flat_worker(settings) if is_flat else None
    flat_invariant_builder = (
        _make_flat_invariant_builder(
            settings=settings,
            prompt_builder=prompt_builder,
            context_file=context_file,
        )
        if is_flat
        else None
    )

    app = get_application(
        infra,
        ApplicationConfig(
            topology=settings.orchestration.topology,
            concurrency=settings.orchestration.concurrency,
            tool_calling=_tool_calling_config(settings, active_domain_components),
            max_retries=settings.orchestration.max_retries,
            poll_interval=settings.orchestration.poll_interval,
            max_run_duration_seconds=settings.orchestration.max_run_duration_seconds,
            max_redecompositions=settings.orchestration.max_redecompositions,
            skip_judge=settings.orchestration.skip_judge,
            workspace_listing_dirs=(
                tuple(settings.orchestration.workspace_listing_dirs)
                if settings.orchestration.workspace_listing_dirs is not None
                else None
            ),
            workspace_listing_max_entries=settings.orchestration.workspace_listing_max_entries,
            verification_max_retries=settings.orchestration.verification_max_retries,
            capture_recon_reads=settings.orchestration.capture_recon_reads,
            share_boss_recon=settings.orchestration.share_boss_recon,
            procedural_dispatch=settings.orchestration.procedural_dispatch,
            treatment_version=settings.orchestration.treatment_version,
            config_hash=_effective_config_hash(settings),
            boss_config=settings.boss,
            manager_config=settings.manager,
            output_directory=settings.output.directory,
            default_worker_tool=settings.worker.tool,
            domain_plugin=plugin,
            prompt_strategy=active_domain_components.prompt_strategy,
            prompt_builder=prompt_builder,
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
