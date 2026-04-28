"""Composition helpers for bootstrap runtime wiring."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.application.execution_service import FlatModeBundle
from core.application.run_invariants import (
    build_task_prompt,
    build_timeouts,
    build_tool_policy,
    build_workspace_spec,
)
from infrastructure.adapters.worker import OpenHandsAdapter
from infrastructure.workers import ClaudeCodeWorker, OpenHandsWorker
from plugins.security import SecurityDomainPlugin
from plugins.security.docker_runtime import DockerSecBenchRuntime
from presentation.cli import CLI, CLIConfig

from .application import ApplicationConfig, get_application
from .infrastructure import InfrastructureConfig, get_infrastructure


logger = logging.getLogger(__name__)

# Fields that MUST NEVER reach a prompt. The gold patch and any candidate
# fix bodies are evaluation-only artifacts — leaking them invalidates the
# A/B/C comparison and tutors the model with the answer key.
#
# CVEInstance.to_template_context() already filters these from the
# materialized-dict path. This helper covers the secondary path where a
# raw JSON fixture file is embedded verbatim (legacy byte-identity path
# in build_task_prompt) — that text never goes through CVEInstance, so
# the strip must happen here before it crosses the prompt boundary.
_FORBIDDEN_PROMPT_CONTEXT_FIELDS: frozenset[str] = frozenset({"patch", "candidate_fixes"})


def _strip_forbidden_text(raw_text: str | None) -> str | None:
    """Re-serialize CVE JSON with the forbidden fields removed.

    Fast path: when no forbidden fields are present, return the input
    verbatim so the legacy byte-identity contract still holds for benign
    fixtures. Re-serialize only when a strip is actually needed.

    Returns ``None`` when the input cannot be safely filtered (missing
    or non-object JSON). Callers must NOT fall back to the raw text in
    that case — it may still embed the gold patch.
    """
    if raw_text is None:
        return None
    try:
        parsed: Any = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "CVE context text is not valid JSON; refusing to embed it raw"
            " — would risk leaking the gold patch."
        )
        return None
    if not isinstance(parsed, dict):
        logger.warning(
            "CVE context is JSON but not an object (got %s); refusing to embed.",
            type(parsed).__name__,
        )
        return None
    if not _FORBIDDEN_PROMPT_CONTEXT_FIELDS.intersection(parsed):
        return raw_text
    for field in _FORBIDDEN_PROMPT_CONTEXT_FIELDS:
        parsed.pop(field, None)
    # ensure_ascii=False preserves non-ASCII content identically to the
    # fast path, so the byte-identity contract holds for unicode bodies
    # whose only difference from the fast-path output is the strip.
    return json.dumps(parsed, indent=2, ensure_ascii=False)


if TYPE_CHECKING:
    from collections.abc import Callable

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


_BRIEFING_RELATIVE = Path("prompts/domains/secbench/briefing.md")


def _resolve_briefing_path() -> Path:
    """Locate the canonical SEC-bench briefing relative to the repo root.

    The briefing markdown ships in ``prompts/domains/secbench/`` and is the
    single source of task framing shared between flat and hierarchical modes.
    """
    return Path(__file__).resolve().parents[1] / _BRIEFING_RELATIVE


def _build_flat_worker(settings: Settings) -> WorkerPort:
    """Pick the WorkerPort adapter implied by ``settings.worker.tool``.

    google_adk has no flat-mode adapter today; it raises a clear NotImplementedError
    so configuration errors surface at bootstrap rather than at dispatch time.
    """
    tool = settings.worker.tool
    if tool == "claude_code":
        params = settings.worker.tool_params.claude_code
        if params is None:
            raise ValueError(
                "worker.tool_params.claude_code must be populated for orchestration.mode='flat' "
                "with worker.tool='claude_code'"
            )
        return ClaudeCodeWorker(
            model=settings.worker.model,
            output_format=params.output_format,
            include_partial_messages=params.include_partial_messages,
            max_turns=params.max_turns,
            use_global_config=params.use_global_config,
        )
    if tool == "openhands":
        adapter = OpenHandsAdapter(
            model=settings.worker.model,
            timeout_seconds=settings.worker.timeout,
            max_iterations_per_run=settings.worker.max_iterations_per_run,
            base_url=settings.worker.base_url,
            allowed_tools=settings.worker.allowed_tools,
            disallowed_tools=settings.worker.disallowed_tools,
        )
        return OpenHandsWorker(adapter=adapter)
    if tool == "google_adk":
        raise NotImplementedError("flat-mode worker for google_adk is not implemented yet")
    raise ValueError(f"Unknown worker tool: {tool}")


def _make_flat_invariant_builder(
    *,
    settings: Settings,
    context_file: Path | None,
) -> FlatInvariantBuilder:
    """Return a ``FlatInvariantBuilder`` closure for the active run.

    Bootstrap captures the static settings + the optional CVE fixture path and
    materializes a ``FlatModeBundle`` per call. The plugin-supplied
    ``domain_context`` (e.g. ``CVEInstance``) is passed through to
    ``build_task_prompt`` for provenance.
    """
    briefing_path = _resolve_briefing_path()

    def _build(
        *,
        task: str,
        domain_context: object | None,
        run_dir: Path,
    ) -> FlatModeBundle:
        raw_cve_text: str | None = None
        cve_context: dict[str, object] | None = None
        cve_context_name = "context.json"
        if context_file is not None:
            try:
                raw_cve_text = context_file.read_text(encoding="utf-8")
            except OSError:
                raw_cve_text = None
            cve_context_name = context_file.name

        if domain_context is not None and hasattr(domain_context, "to_template_context"):
            try:
                materialized = domain_context.to_template_context()  # type: ignore[attr-defined]
            except Exception:
                materialized = None
            if isinstance(materialized, dict):
                cve_context = materialized

        # Strip evaluation-only fields (gold patch, candidate fixes) from
        # the raw-text path. The materialized-dict path is already
        # filtered by CVEInstance.to_template_context().
        cve_context_text = _strip_forbidden_text(raw_cve_text)

        spec = build_task_prompt(
            briefing_path=briefing_path,
            cve_context=cve_context,
            cve_context_text=cve_context_text,
            cve_context_name=cve_context_name,
            task=task,
        )
        tool_policy = build_tool_policy(settings=settings)
        timeouts = build_timeouts(settings)
        workspace = build_workspace_spec(run_dir=run_dir)
        return FlatModeBundle(
            spec=spec,
            tool_policy=tool_policy,
            timeouts=timeouts,
            workspace=workspace,
        )

    return _build


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
            worker_allowed_tools=settings.worker.allowed_tools,
            worker_disallowed_tools=settings.worker.disallowed_tools,
            worker_tool_max_iterations=settings.worker.max_iterations_per_run,
            worker_tool_base_url=settings.worker.base_url,
            format_repairer_enabled=settings.format_repairer.enabled,
            format_repairer_model=settings.format_repairer.model,
            format_repairer_max_tokens=settings.format_repairer.max_tokens,
            format_repairer_api_base=settings.format_repairer.api_base,
        )
    )

    plugin = active_domain_components.plugin

    flat_worker: WorkerPort | None = None
    flat_invariant_builder: FlatInvariantBuilder | None = None
    if settings.orchestration.mode == "flat":
        flat_worker = _build_flat_worker(settings)
        flat_invariant_builder = _make_flat_invariant_builder(
            settings=settings,
            context_file=context_file,
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
