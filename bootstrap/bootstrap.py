"""Bootstrap - Composition Root: argument parsing, dependency wiring, command dispatch."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from config import Settings
from core.domain.events.events import RunStarted
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from infrastructure.cleanup.registry import CleanupRegistry
from infrastructure.snapshot import snapshot_effective_config

from .composition import (
    available_domain_names,
    create_runtime_cli,
    get_first_enabled_domain_components,
    get_run_domain_components,
)


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from core.ports.domain_plugin_port import DomainPlugin


def main(args: list[str] | None = None) -> None:
    """Entry point - parse args and dispatch to command handler."""
    parser = _create_parser()
    parsed = parser.parse_args(args)

    if not parsed.command:
        parser.print_help()
        sys.exit(0)

    # Install signal/atexit cleanup hooks once at process entry. Idempotent
    # internally, but main() runs once per invocation so a fresh registry
    # per process is the natural lifetime. Registry is threaded into every
    # command handler so domain plugins can register
    # SIGKILL-recovery cleanup callbacks at composition time.
    cleanup_registry = CleanupRegistry()
    cleanup_registry.install(signals=(signal.SIGTERM, signal.SIGINT))

    handlers: dict[str, Callable[..., Coroutine[Any, Any, None]]] = {
        "run": _run_task,
        "events": partial(_query_projection, output_type="events"),
        "summary": partial(_query_projection, output_type="summary"),
        "list": _list_runs,
        "prompts": _trace_prompts,
    }

    try:
        asyncio.run(handlers[parsed.command](parsed, cleanup_registry=cleanup_registry))
    except KeyboardInterrupt:
        # Defensive backstop: install() consumes SIGINT via signal.signal,
        # but asyncio.run can still translate a SIGINT delivered during a
        # tight C-extension call into KeyboardInterrupt. run_all() is
        # idempotent — calling it again after a signal already fired is a
        # no-op because the registry clears itself on the first sweep.
        cleanup_registry.run_all()
        print("\nInterrupted. Agent state is persisted in the event store.")
        sys.exit(0)


def _load_settings(config_path: Path | None) -> Settings:
    return Settings.from_yaml(config_path) if config_path else Settings.load()


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arise",
        description=(
            "Arise Sec Lion - A recursive, self-healing multi-agent orchestration platform."
        ),
    )
    parser.add_argument("-c", "--config", type=Path, help="Path to YAML configuration file")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    p = sub.add_parser("run", help="Run a task with the multi-agent system")
    p.add_argument("task", help="The task description to execute")
    p.add_argument(
        "--domain-context-file",
        type=Path,
        help="Path to domain context JSON file for plugin-specific runs",
    )
    p.add_argument(
        "--domain",
        type=str,
        choices=available_domain_names(),
        help="Enable an optional domain plugin for this run",
    )
    p.add_argument(
        "--worker-model",
        type=str,
        help="Override worker LLM model (e.g., gpt-4o, claude-sonnet-4-20250514)",
    )
    p.add_argument(
        "--worker-tool",
        type=str,
        choices=["claude_code", "openhands", "google_adk"],
        help="Override worker execution tool (claude_code, openhands, google_adk)",
    )

    projection_commands: list[tuple[str, str, list[tuple[str, dict[str, Any]]]]] = [
        ("events", "View events for a task run", [("--errors-only", {"action": "store_true"})]),
        ("summary", "Show summary projection for a task run", []),
    ]
    for name, help_text, extra_args in projection_commands:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--agent-id", type=UUID, help="Agent UUID (default: last run)")
        p.add_argument("-o", "--output", type=Path, help="Output file path")
        p.add_argument(
            "--format",
            choices=(
                ["json", "text"] if name == "summary" else ["json", "jsonl", "text", "compact"]
            ),
            default="json",
        )
        for arg_name, arg_opts in extra_args:
            p.add_argument(arg_name, **arg_opts)

    p = sub.add_parser("list", help="List past BOSS agent runs")
    p.add_argument("--limit", type=int, default=10, help="Number of runs to show")
    p.add_argument("--format", choices=["json", "text"], default="text")

    # Prompt trace command (simplified)
    p = sub.add_parser("prompts", help="Trace prompts through agent hierarchy")
    p.add_argument("--agent-id", type=UUID, help="Agent UUID (default: last run)")
    p.add_argument("--format", choices=["tree", "json"], default="tree", help="Output format")

    return parser


@asynccontextmanager
async def _event_store(settings: Settings) -> AsyncIterator[PostgresEventStore]:
    store = PostgresEventStore(
        settings.database.connection_string,
        pool_min=settings.database.pool_min,
        pool_max=settings.database.pool_max,
    )
    await store.connect()
    try:
        yield store
    finally:
        await store.disconnect()


async def _run_task(
    args: argparse.Namespace,
    *,
    cleanup_registry: CleanupRegistry | None = None,
) -> None:
    from core.query.projections import ProjectionPipelineBuilder
    from presentation.formatters import ProgressDisplayFormatter
    from presentation.persistence import RunPersistence

    settings = _apply_worker_overrides(_load_settings(args.config), args)
    callback = ProgressDisplayFormatter.display if settings.output.verbose else None

    context_file = getattr(args, "domain_context_file", None)
    domain_components = get_run_domain_components(
        settings,
        requested_domain=getattr(args, "domain", None),
        context_file=context_file,
    )
    domain_context = _infer_domain_context(
        plugin=domain_components.plugin,
        task_text=args.task,
        context_file=context_file,
    )
    _print_inferred_domain_context(
        plugin=domain_components.plugin,
        domain_context=domain_context,
        verbose=settings.output.verbose,
    )

    cli = create_runtime_cli(
        settings,
        progress_callback=callback,
        domain_components=domain_components,
        context_file=context_file,
        cleanup_registry=cleanup_registry,
    )

    # Wall-clock timings cover runtime setup (domain plugin init, docker
    # workspace prep) and execution up to cli.run_task() returning. Post-run
    # projection is NOT included because it can fail independently of the run
    # succeeding.
    wall_started_at = datetime.now(UTC)
    result = await cli.run_task(args.task, domain_context=domain_context)
    wall_ended_at = datetime.now(UTC)

    # Post-run projection is best-effort: if Postgres is momentarily unhappy
    # the manifest still lands (with summary_available=false) so the harness's
    # register_run step has something to enroll. A completed run must not be
    # blocked from study enrollment by a transient projection failure, and
    # consumers can tell "genuinely zero" from "projection failed" via
    # summary_available rather than faked zero tokens/costs.
    summary = None
    try:
        async with _event_store(settings) as store:
            pipeline = ProjectionPipelineBuilder(store).with_output("summary").build()
            summary = await pipeline.execute_summary(root_agent_id=result.root_id)
    except Exception:
        logger.exception(
            "Failed to project run summary for %s; manifest will record summary_available=false",
            result.root_id,
        )

    persistence = RunPersistence(Path(settings.output.directory))
    domain_context_path = Path(context_file) if context_file else None
    try:
        await persistence.write_run_manifest(
            result.root_id,
            settings=settings,
            task=args.task,
            domain_context_path=domain_context_path,
            exit_status=result.status,
            wall_started_at=wall_started_at,
            wall_ended_at=wall_ended_at,
            summary=summary,
            artifact_subdirectory=domain_components.artifact_subdirectory,
        )
    except Exception:
        logger.exception(
            "Failed to write run manifest for %s; run outcome: %s",
            result.root_id,
            result.status,
        )

    # Snapshot the resolved Settings into the run directory so future
    # researchers can reproduce the exact config that produced this run.
    # Best-effort: a snapshot failure must not block run completion.
    try:
        snapshot_effective_config(
            run_id=result.root_id,
            settings=settings,
            run_dir=Path(settings.output.directory) / str(result.root_id),
        )
    except Exception:
        logger.exception(
            "Failed to snapshot effective config for %s; manifest still written",
            result.root_id,
        )

    # Preserve pre-change behavior: a non-success run exits non-zero so
    # automation (study harness, CI) can distinguish passing from failing runs.
    if result.status != "success":
        sys.exit(1)


def _infer_domain_context(
    *,
    plugin: DomainPlugin | None,
    task_text: str,
    context_file: object | None,
) -> object | None:
    if plugin is None:
        return None

    return plugin.infer_context(
        task_text,
        context_file=context_file,
        fail_fast=True,
    )


def _print_inferred_domain_context(
    *,
    plugin: DomainPlugin | None,
    domain_context: object | None,
    verbose: bool,
) -> None:
    if plugin is None or domain_context is None or not verbose:
        return

    metadata = plugin.get_run_metadata(domain_context)
    instance_id = metadata.get("instance_id")
    if isinstance(instance_id, str):
        print(f"[Inferred] Domain context: {instance_id}")


def _apply_worker_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    """Apply CLI overrides for worker model and tool.

    Args:
        settings: Base settings from config file.
        args: Parsed CLI arguments.

    Returns:
        Settings with CLI overrides applied (creates new instance if changes needed).
    """
    worker_model = getattr(args, "worker_model", None)
    worker_tool = getattr(args, "worker_tool", None)

    if not worker_model and not worker_tool:
        return settings  # No overrides

    new_worker = settings.worker.model_copy(
        update={
            **({"model": worker_model} if worker_model else {}),
            **({"tool": worker_tool} if worker_tool else {}),
        }
    )

    # Return new settings with updated worker config
    return settings.model_copy(update={"worker": new_worker})


async def _query_projection(
    args: argparse.Namespace,
    output_type: str,
    *,
    cleanup_registry: CleanupRegistry | None = None,
) -> None:
    del cleanup_registry  # Query path has no cleanup obligations.
    from core.query.projections import ProjectionPipelineBuilder

    settings = _load_settings(args.config)
    agent_id = _resolve_agent_id(settings, args.agent_id)
    if agent_id is None:
        return

    async with _event_store(settings) as store:
        builder = ProjectionPipelineBuilder(store)
        if output_type == "summary":
            builder = builder.with_output("summary")
        elif getattr(args, "errors_only", False):
            builder = builder.with_filter("errors_only")

        builder = builder.with_formatter(args.format)
        if args.output:
            await builder.to_sink("file", path=str(args.output)).build().execute(agent_id)
            print(f"Written to {args.output}")
        else:
            await builder.to_sink("stdout").build().execute(agent_id)


async def _list_runs(
    args: argparse.Namespace,
    *,
    cleanup_registry: CleanupRegistry | None = None,
) -> None:
    del cleanup_registry  # Query path has no cleanup obligations.
    from core.domain.aggregates.agent_session import AgentRole
    from core.domain.events.events import AgentCreated
    from presentation.rendering import OutputRenderer

    settings = _load_settings(args.config)

    async with _event_store(settings) as store:
        grouped = await store.get_all_events_grouped()
        runs = []
        for aid, evts in grouped.items():
            if not evts or not isinstance(evts[0], AgentCreated):
                continue
            if evts[0].role != AgentRole.BOSS.value:
                continue

            run_started = next((e for e in evts if isinstance(e, RunStarted)), None)
            task = run_started.task_description if run_started is not None else "N/A"
            runs.append(
                {
                    "boss_id": str(aid),
                    "task": task[:50] + ("..." if len(task) > 50 else ""),
                    "started_at": evts[0].occurred_at.isoformat(),
                    "event_count": len(evts),
                    "status": type(evts[-1]).__name__,
                }
            )
        runs = sorted(runs, key=lambda x: str(x["started_at"]), reverse=True)[: args.limit]

        if args.format == "json":
            print(json.dumps(runs, indent=2))
        else:
            OutputRenderer.print_runs_table(runs)


async def _trace_prompts(
    args: argparse.Namespace,
    *,
    cleanup_registry: CleanupRegistry | None = None,
) -> None:
    del cleanup_registry  # Query path has no cleanup obligations.
    from core.application.services import PromptParser, PromptTraceService
    from core.domain.values.prompt_trace import RenderOptions
    from presentation.formatters.prompt_trace_formatter import get_renderer

    settings = _load_settings(args.config)
    agent_id = _resolve_agent_id(settings, args.agent_id)
    if agent_id is None:
        return

    async with _event_store(settings) as store:
        domain_plugin = get_first_enabled_domain_components(settings).plugin
        parser = PromptParser(
            extra_tag_mappings=domain_plugin.get_tag_mappings() if domain_plugin else None,
            extra_provenance_patterns=(
                domain_plugin.get_provenance_patterns() if domain_plugin else None
            ),
        )
        service = PromptTraceService(store, parser)
        trace = await service.trace(agent_id)

        # Render output (simplified: no filtering options)
        renderer = get_renderer(args.format)
        output = renderer.render(trace, RenderOptions())
        print(output)


def _resolve_agent_id(
    settings: Settings,
    requested_agent_id: UUID | None,
) -> UUID | None:
    from presentation.persistence import RunPersistence

    agent_id = (
        requested_agent_id or RunPersistence(Path(settings.output.directory)).get_last_run_id()
    )
    if agent_id is None:
        print("Error: No agent-id specified and no last run found.")
    return agent_id
