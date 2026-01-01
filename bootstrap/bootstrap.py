"""Bootstrap - Composition Root: argument parsing, dependency wiring, command dispatch."""



import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from config import Settings
from infrastructure.adapters.postgres_event_store import PostgresEventStore
from presentation.cli import CLI, CLIConfig

from .application import ApplicationConfig, get_application
from .infrastructure import InfrastructureConfig, get_infrastructure


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
    }

    try:
        asyncio.run(handlers[parsed.command](parsed))
    except KeyboardInterrupt:
        print("\nInterrupted. Agent state is persisted in the event store.")
        sys.exit(0)


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arise",
        description="Arise Sec Lion - A recursive, self-healing multi-agent orchestration platform.",
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
        p.add_argument("--format", choices=["json", "text"] if name == "summary" else ["json", "jsonl", "text", "compact"], default="json")
        for arg_name, arg_opts in extra_args:
            p.add_argument(arg_name, **arg_opts)

    p = sub.add_parser("list", help="List past BOSS agent runs")
    p.add_argument("--limit", type=int, default=10, help="Number of runs to show")
    p.add_argument("--format", choices=["json", "text"], default="text")

    return parser


@asynccontextmanager
async def _event_store(settings: Settings):
    """Create and manage event store lifecycle."""
    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        yield store
    finally:
        await store.disconnect()


async def _run_task(args: argparse.Namespace) -> None:
    from presentation.formatters import ProgressDisplayFormatter

    settings = Settings.from_yaml(args.config) if args.config else Settings.load()

    # Apply CLI overrides for worker configuration
    settings = _apply_worker_overrides(settings, args)

    callback = ProgressDisplayFormatter.display if settings.output.verbose else None
    cve_file = getattr(args, "cve_file", None)
    await _create_cli(settings, callback).run_task(args.task, cve_file=cve_file)


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
    from presentation.persistence import RunPersistence

    settings = Settings.from_yaml(args.config) if args.config else Settings.load()
    agent_id = args.agent_id or RunPersistence(Path(settings.output.directory)).get_last_run_id()

    if not agent_id:
        print("Error: No agent-id specified and no last run found.")
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
    from core.domain.events.events import AgentCreated
    from core.domain.aggregates.agent_session import AgentRole
    from presentation.rendering import OutputRenderer

    settings = Settings.from_yaml(args.config) if args.config else Settings.load()

    async with _event_store(settings) as store:
        grouped = await store.get_all_events_grouped()
        runs = [
            {
                "boss_id": str(aid),
                "task": (t := next((e.task_description for e in evts if hasattr(e, "task_description")), "N/A"))[:50] + ("..." if len(t) > 50 else ""),
                "started_at": evts[0].occurred_at.isoformat(),
                "event_count": len(evts),
                "status": type(evts[-1]).__name__,
            }
            for aid, evts in grouped.items()
            if evts and isinstance(evts[0], AgentCreated) and evts[0].role == AgentRole.BOSS.value
        ]
        runs = sorted(runs, key=lambda x: x["started_at"], reverse=True)[:args.limit]

        print(json.dumps(runs, indent=2)) if args.format == "json" else OutputRenderer.print_runs_table(runs)


def _create_cli(settings: Settings, progress_callback=None):
    infra = get_infrastructure(InfrastructureConfig(
        postgres_connection_string=settings.database.connection_string,
        default_worker_tool=settings.worker.tool,
        worker_tool_model=settings.worker.model,
        worker_tool_timeout=settings.worker.timeout,
    ))

    app = get_application(infra, ApplicationConfig(
        system_limits=settings.orchestration.limits,
        max_retries=settings.orchestration.max_retries,
        poll_interval=settings.orchestration.poll_interval,
        boss_config=settings.boss,
        manager_config=settings.manager,
        output_directory=settings.output.directory,
        default_worker_tool=settings.worker.tool,
        progress_callback=progress_callback,
        orchestration_config=settings.orchestration,
    ))

    return CLI(
        execution_service=app.execution_service,
        config=CLIConfig(
            verbose=settings.output.verbose,
            output_directory=settings.output.directory,
            default_worker_tool=settings.worker.tool,
        ),
    )


