"""Bootstrap - Composition Root: argument parsing, dependency wiring, command dispatch."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from config import Settings
from infrastructure.adapters.postgres_event_store import PostgresEventStore

from .composition import (
    create_runtime_cli,
    get_first_enabled_domain_components,
    get_run_domain_components,
)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core.ports.domain_plugin_port import DomainPlugin


def main(args: list[str] | None = None) -> None:
    """Entry point - parse args and dispatch to command handler."""
    parser = _create_parser()
    parsed = parser.parse_args(args)

    if not parsed.command:
        parser.print_help()
        sys.exit(0)

    handlers = {
        "run": _run_task,
        "events": lambda a: _query_projection(a, "events"),
        "summary": lambda a: _query_projection(a, "summary"),
        "list": _list_runs,
        "prompts": _trace_prompts,
    }

    try:
        asyncio.run(handlers[parsed.command](parsed))
    except KeyboardInterrupt:
        print("\nInterrupted. Agent state is persisted in the event store.")
        sys.exit(0)


def _load_settings(config_path: Path | None) -> Settings:
    """Load settings from a provided config path or the default location."""
    return Settings.from_yaml(config_path) if config_path else Settings.load()


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arise",
        description=(
            "Arise Sec Lion - A recursive, self-healing multi-agent orchestration "
            "platform."
        ),
    )
    parser.add_argument("-c", "--config", type=Path, help="Path to YAML configuration file")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    p = sub.add_parser("run", help="Run a task with the multi-agent system")
    p.add_argument("task", help="The task description to execute")
    p.add_argument(
        "--cve-file",
        type=Path,
        help="Path to SEC-bench CVE instance JSON file for benchmark runs",
    )
    p.add_argument(
        "--domain",
        type=str,
        choices=["security"],
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

    for name, help_text, extra_args in [
        ("events", "View events for a task run", [("--errors-only", {"action": "store_true"})]),
        ("summary", "Show summary projection for a task run", []),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--agent-id", type=UUID, help="Agent UUID (default: last run)")
        p.add_argument("-o", "--output", type=Path, help="Output file path")
        p.add_argument(
            "--format",
            choices=(
                ["json", "text"]
                if name == "summary"
                else ["json", "jsonl", "text", "compact"]
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
    """Create and manage event store lifecycle."""
    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        yield store
    finally:
        await store.disconnect()


async def _run_task(args: argparse.Namespace) -> None:
    from presentation.formatters import ProgressDisplayFormatter

    settings = _apply_worker_overrides(_load_settings(args.config), args)
    callback = ProgressDisplayFormatter.display if settings.output.verbose else None

    domain_components = get_run_domain_components(
        settings,
        requested_domain=getattr(args, "domain", None),
        cve_file=getattr(args, "cve_file", None),
    )
    domain_context = _infer_domain_context(
        plugin=domain_components.plugin,
        task_text=args.task,
        cve_file=getattr(args, "cve_file", None),
    )
    _print_inferred_domain_context(
        plugin=domain_components.plugin,
        domain_context=domain_context,
        verbose=settings.output.verbose,
    )

    await create_runtime_cli(
        settings,
        progress_callback=callback,
        domain_components=domain_components,
    ).run_task(args.task, domain_context=domain_context)


def _infer_domain_context(
    *,
    plugin: DomainPlugin | None,
    task_text: str,
    cve_file: object | None,
) -> object | None:
    """Infer optional domain context for a run."""
    if plugin is None:
        return None

    return plugin.infer_context(
        task_text,
        cve_file=cve_file,
        fail_fast=True,
    )


def _print_inferred_domain_context(
    *,
    plugin: DomainPlugin | None,
    domain_context: object | None,
    verbose: bool,
) -> None:
    """Print inferred domain metadata when verbose output is enabled."""
    if plugin is None or domain_context is None or not verbose:
        return

    metadata = plugin.get_run_metadata(domain_context)
    instance_id = metadata.get("instance_id")
    if isinstance(instance_id, str):
        print(f"[Inferred] SEC-bench CVE: {instance_id}")


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

    # Build updated worker config
    new_worker = settings.worker.model_copy(update={
        **({"model": worker_model} if worker_model else {}),
        **({"tool": worker_tool} if worker_tool else {}),
    })

    # Return new settings with updated worker config
    return settings.model_copy(update={"worker": new_worker})


async def _query_projection(args: argparse.Namespace, output_type: str) -> None:
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


async def _list_runs(args: argparse.Namespace) -> None:
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

            task = next(
                (e.task_description for e in evts if hasattr(e, "task_description")),
                "N/A",
            )
            runs.append(
                {
                    "boss_id": str(aid),
                    "task": task[:50] + ("..." if len(task) > 50 else ""),
                    "started_at": evts[0].occurred_at.isoformat(),
                    "event_count": len(evts),
                    "status": type(evts[-1]).__name__,
                }
            )
        runs = sorted(runs, key=lambda x: x["started_at"], reverse=True)[:args.limit]

        if args.format == "json":
            print(json.dumps(runs, indent=2))
        else:
            OutputRenderer.print_runs_table(runs)


async def _trace_prompts(args: argparse.Namespace) -> None:
    """Trace prompts through agent hierarchy."""
    from core.application.services.prompt_parser import PromptParser
    from core.application.services.prompt_trace_service import PromptTraceService
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
    """Resolve an explicit agent id or fall back to the last persisted run."""
    from presentation.persistence import RunPersistence

    agent_id = (
        requested_agent_id
        or RunPersistence(Path(settings.output.directory)).get_last_run_id()
    )
    if agent_id is None:
        print("Error: No agent-id specified and no last run found.")
    return agent_id
