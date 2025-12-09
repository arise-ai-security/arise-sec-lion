"""CLI Interface: run, events, summary, list commands via Click."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import click


if TYPE_CHECKING:
    from core.application.execution_service import AgentExecutionService
    from core.application.projections import ProjectionPipelineBuilder
    from core.ports.event_store_port import EventStorePort


@dataclass
class CLIConfig:
    verbose: bool = True
    output_directory: str = "./output"


def format_event_progress(event: object, _agent: object) -> None:  # noqa: PLR0912
    """Print real-time progress for domain events (duck-typed callback)."""
    event_type = type(event).__name__
    agent_id = str(getattr(event, "aggregate_id", "?"))[:8]  # Short ID

    match event_type:
        case "AgentCreated":
            role = getattr(event, "role", "?")
            parent_id = getattr(event, "parent_id", None)
            parent_info = f" (parent: {str(parent_id)[:8]})" if parent_id else " (root)"
            print(f"   🤖 Agent {agent_id}... created as {role}{parent_info}")
        case "TaskAssigned":
            task = getattr(event, "task_description", "")
            task_preview = task[:60] + "..." if len(task) > 60 else task
            print(f"   📋 [{agent_id}] Task: {task_preview}")
        case "StatusChanged":
            old = getattr(event, "old_status", "?")
            new = getattr(event, "new_status", "?")
            reason = getattr(event, "reason", "")
            reason_info = f" ({reason})" if reason else ""
            print(f"   → [{agent_id}] {old} → {new}{reason_info}")
        case "ComplexityEvaluated":
            complexity = getattr(event, "complexity", "?")
            role = getattr(event, "determined_role", "?")
            emoji = "🔧" if complexity == "simple" else "🔀"
            print(f"   {emoji} [{agent_id}] Complexity: {complexity} → becomes {role.upper()}")
        case "SubtasksDefined":
            subtasks = getattr(event, "subtasks", [])
            print(f"   📑 [{agent_id}] Decomposed into {len(subtasks)} subtask(s)")
        case "ChildSpawned":
            child_id = str(getattr(event, "child_id", "?"))[:8]
            child_role = getattr(event, "child_role", "?")
            subtask = getattr(event, "subtask", None)
            desc = getattr(subtask, "description", "")[:40] if subtask else ""
            print(f"   👶 [{agent_id}] Spawned child {child_id}... as {child_role}")
            if desc:
                print(f"      └─ {desc}...")
        case "CodeGenerationStarted":
            tool = getattr(event, "tool_name", "?")
            print(f"   ⚡ [{agent_id}] Code generation started (tool: {tool})")
        case "ThoughtCaptured":
            pass
        case "WorkCompleted":
            print(f"   ✅ [{agent_id}] Work completed")
        case "WorkFailed":
            reason = getattr(event, "reason", "unknown")[:80]
            print(f"   ❌ [{agent_id}] Work failed: {reason}")
        case "ChildCompleted":
            child_id = str(getattr(event, "child_id", "?"))[:8]
            print(f"   ✓ [{agent_id}] Child {child_id}... completed")
        case _:
            print(f"   • [{agent_id}] {event_type}")


def get_last_run(output_dir: Path) -> dict | None:
    path = output_dir / ".last_run.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_last_run(output_dir: Path, boss_id: UUID, task: str, status: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "boss_id": str(boss_id),
        "task": task,
        "started_at": datetime.now().isoformat(),
        "status": status,
    }
    path = output_dir / ".last_run.json"
    path.write_text(json.dumps(data, indent=2))


def get_last_run_id(output_dir: Path) -> UUID | None:
    last_run = get_last_run(output_dir)
    if last_run and "boss_id" in last_run:
        return UUID(last_run["boss_id"])
    return None


class CLI:
    """Encapsulates task execution workflow: init → run → display → cleanup."""

    def __init__(
        self,
        execution_service: AgentExecutionService,
        config: CLIConfig | None = None,
    ) -> None:
        self.execution_service = execution_service
        self.config = config or CLIConfig()
        self._output_dir = Path(self.config.output_directory)

    def _print_banner(self) -> None:
        print("╔═══════════════════════════════════════════════════════════════════╗")
        print("║  Recursive Multi-Agent System - Powered by Event Sourcing       ║")
        print("╚═══════════════════════════════════════════════════════════════════╝")
        print()

    def _print_usage(self) -> None:
        self._print_banner()
        print("ERROR: No task provided!")
        print()
        print("Usage:")
        print('  python main.py run "<your task description>"')
        print()
        print("Examples:")
        print('  python main.py run "Build a REST API with authentication"')
        print('  python main.py run "Analyze this codebase for security issues"')
        print('  python main.py run "Refactor the payment module to use Strategy pattern"')
        print()
        print("Other commands:")
        print("  python main.py events    - View events for last run")
        print("  python main.py summary   - Show summary projection")
        print("  python main.py list      - List past BOSS agent runs")
        print()

    def _parse_arguments(self) -> str | None:
        if len(sys.argv) < 2:
            return None
        return " ".join(sys.argv[1:])

    async def _initialize_infrastructure(self) -> None:
        if self.config.verbose:
            print("[1/5] Initializing infrastructure adapters...")
            print("   ✓ Event Store: PostgreSQL")
            print("   ✓ LLM Adapter: LiteLLM")
            print("   ✓ Worker Tools: Claude Code PTY, OpenHands (routed by config)")
            print()

        if self.config.verbose:
            print("[2/5] Connecting to PostgreSQL and initializing schema...")

        try:
            await self.execution_service.initialize()
            if self.config.verbose:
                print("   ✓ Connected to PostgreSQL")
                print("   ✓ Schema initialized")
                print()
        except Exception as e:
            print(f"   ✗ Failed to connect to database: {e}")
            print()
            print("Make sure PostgreSQL is running:")
            print("  docker compose up -d postgres")
            print()
            sys.exit(1)

    async def _bootstrap_boss_agent(self, task_description: str) -> UUID:
        if self.config.verbose:
            print("[3/5] Creating root BOSS agent...")

        try:
            root_id = await self.execution_service.create_boss_agent(
                task_description=task_description
            )
            if self.config.verbose:
                print(f"   ✓ BOSS Agent Created (ID: {root_id})")
                print(f"   ✓ Task Assigned: {task_description}")
                print()
            return root_id
        except Exception as e:
            print(f"   ✗ Failed to create BOSS agent: {e}")
            await self.execution_service.cleanup()
            sys.exit(1)

    async def _run_orchestration_loop(self, root_id: UUID) -> None:
        if self.config.verbose:
            print("[4/5] Starting orchestration loop...")
            print("   (This may take a while depending on task complexity)")
            print()

        try:
            await self.execution_service.run_system_loop(root_id)
            if self.config.verbose:
                print("   ✓ All agents completed execution")
                print()
        except Exception as e:
            print(f"   ✗ Orchestration loop failed: {e}")
            await self.execution_service.cleanup()
            sys.exit(1)

    async def _display_final_result(self, root_id: UUID) -> None:
        if self.config.verbose:
            print("[5/5] Fetching final result...")

        try:
            final_result = await self.execution_service.get_agent_result(root_id)

            print()
            print("╔═══════════════════════════════════════════════════════════════════╗")
            print("║  FINAL RESULT                                                    ║")
            print("╚═══════════════════════════════════════════════════════════════════╝")
            print()

            if final_result.result:
                print(final_result.result)
            else:
                print("(No result produced)")

            print()
            print(f"Status: {final_result.status}")
            print()

            stats = await self.execution_service.get_system_statistics()
            print(f"Total Agents Created: {stats.total_agents}")
            print()

        except Exception as e:
            print(f"   ✗ Failed to fetch final result: {e}")

    async def _cleanup(self) -> None:
        await self.execution_service.cleanup()
        if self.config.verbose:
            print("✓ System shutdown complete")

    async def run_with_task(self, task_description: str) -> None:
        self._print_banner()
        print(f"Task: {task_description}")
        print()

        self._output_dir.mkdir(parents=True, exist_ok=True)
        status = "failed"
        root_id = None

        try:
            await self._initialize_infrastructure()
            root_id = await self._bootstrap_boss_agent(task_description)
            await self._run_orchestration_loop(root_id)
            await self._display_final_result(root_id)
            status = "completed"
        finally:
            if root_id:
                save_last_run(self._output_dir, root_id, task_description, status)
            await self._cleanup()

    async def run(self) -> None:
        task_description = self._parse_arguments()

        if task_description is None:
            self._print_usage()
            sys.exit(1)

        await self.run_with_task(task_description)


_cli_context: dict = {}


def _get_output_dir() -> Path:
    return Path(_cli_context.get("output_directory", "./output"))


async def _get_event_store() -> EventStorePort:
    if "event_store" not in _cli_context:
        from config import Settings
        from infrastructure.adapters.postgres_event_store import PostgresEventStore

        config_path = _cli_context.get("config_path")
        settings = Settings.from_yaml(config_path) if config_path else Settings()
        connection_string = settings.infrastructure.postgres_connection_string
        event_store = PostgresEventStore(connection_string)
        await event_store.connect()
        _cli_context["event_store"] = event_store

    return _cli_context["event_store"]


async def _cleanup_event_store() -> None:
    if "event_store" in _cli_context:
        await _cli_context["event_store"].disconnect()
        del _cli_context["event_store"]


async def _execute_pipeline(
    builder: ProjectionPipelineBuilder,
    agent_id: UUID,
    output: str | None,
) -> None:
    builder = builder.to_sink("file", path=output) if output else builder.to_sink("stdout")

    pipeline = builder.build()
    await pipeline.execute(agent_id)

    if output:
        click.echo(f"Written to {output}")


@click.group()
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True),
    help="Path to YAML configuration file",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Arise Multi-Agent System CLI.

    A recursive, self-healing multi-agent orchestration platform.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    _cli_context["config_path"] = config
    if config:
        from config import Settings

        settings = Settings.from_yaml(config)
        _cli_context["output_directory"] = settings.presentation.output_directory
    else:
        _cli_context["output_directory"] = "./output"


@cli.command()
@click.argument("task")
@click.pass_context
def run(ctx: click.Context, task: str) -> None:
    """Run a task with the multi-agent system.

    TASK is the description of what you want to accomplish.

    Examples:

        python main.py run "Build a REST API with authentication"

        python main.py run "Analyze this codebase for security issues"
    """
    from bootstrap import bootstrap

    config_path = ctx.obj.get("config_path")
    app = bootstrap(config_path=config_path)

    asyncio.run(app.run_with_task(task))


@cli.command()
@click.option(
    "--agent-id",
    type=click.UUID,
    help="Agent UUID (default: last run)",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "jsonl", "text", "compact"]),
    default="json",
    help="Output format",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path",
)
@click.option(
    "--errors-only",
    is_flag=True,
    help="Show only WorkFailed events",
)
def events(
    agent_id: UUID | None,
    fmt: str,
    output: str | None,
    errors_only: bool,
) -> None:
    """View events for a task run.

    Shows all events from an agent hierarchy, optionally filtered.

    Examples:

        python main.py events

        python main.py events --format text

        python main.py events --errors-only --output errors.json
    """
    asyncio.run(_show_events(agent_id, fmt, output, errors_only))


async def _show_events(
    agent_id: UUID | None,
    fmt: str,
    output: str | None,
    errors_only: bool,
) -> None:
    import infrastructure.adapters.sinks  # noqa: F401
    from core.application.projections import ProjectionPipelineBuilder

    try:
        event_store = await _get_event_store()
        output_dir = _get_output_dir()

        # Use last run ID if not specified
        if agent_id is None:
            agent_id = get_last_run_id(output_dir)
            if agent_id is None:
                click.echo("Error: No agent-id specified and no last run found.")
                click.echo("Run a task first or specify --agent-id")
                return

        builder = ProjectionPipelineBuilder(event_store)

        if errors_only:
            builder = builder.with_filter("errors_only")

        builder = builder.with_formatter(fmt)
        await _execute_pipeline(builder, agent_id, output)

    finally:
        await _cleanup_event_store()


@cli.command()
@click.option(
    "--agent-id",
    type=click.UUID,
    help="Agent UUID (default: last run)",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "text"]),
    default="json",
    help="Output format",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path",
)
def summary(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    """Show summary projection for a task run.

    Displays aggregated statistics including event counts,
    error count, and timeline information.

    Examples:

        python main.py summary

        python main.py summary --format text

        python main.py summary --output summary.json
    """
    asyncio.run(_show_summary(agent_id, fmt, output))


async def _show_summary(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    import infrastructure.adapters.sinks  # noqa: F401
    from core.application.projections import ProjectionPipelineBuilder

    try:
        event_store = await _get_event_store()
        output_dir = _get_output_dir()

        if agent_id is None:
            agent_id = get_last_run_id(output_dir)
            if agent_id is None:
                click.echo("Error: No agent-id specified and no last run found.")
                click.echo("Run a task first or specify --agent-id")
                return

        builder = ProjectionPipelineBuilder(event_store)
        builder = builder.with_output("summary")
        builder = builder.with_formatter(fmt)
        await _execute_pipeline(builder, agent_id, output)

    finally:
        await _cleanup_event_store()


@cli.command("list")
@click.option(
    "--limit",
    default=10,
    help="Number of runs to show",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "text"]),
    default="text",
    help="Output format",
)
def list_runs(limit: int, fmt: str) -> None:
    """List past BOSS agent runs.

    Shows recent task executions with their IDs and status.

    Examples:

        python main.py list

        python main.py list --limit 5

        python main.py list --format json
    """
    asyncio.run(_list_runs(limit, fmt))


async def _list_runs(limit: int, fmt: str) -> None:
    from core.domain.events import AgentCreated

    try:
        event_store = await _get_event_store()
        all_ids = await event_store.get_all_aggregate_ids()

        boss_runs = []
        for agent_id in all_ids:
            events = await event_store.get_events(agent_id)
            if not events:
                continue

            first_event = events[0]
            if isinstance(first_event, AgentCreated) and first_event.role.value == "BOSS":
                task = "N/A"
                for event in events:
                    if hasattr(event, "task_description"):
                        task = event.task_description
                        break

                last_event = events[-1]
                status = type(last_event).__name__

                boss_runs.append(
                    {
                        "boss_id": str(agent_id),
                        "task": task[:50] + "..." if len(task) > 50 else task,
                        "started_at": first_event.occurred_at.isoformat(),
                        "event_count": len(events),
                        "status": status,
                    }
                )

        boss_runs.sort(key=lambda x: x["started_at"], reverse=True)
        boss_runs = boss_runs[:limit]

        if fmt == "json":
            click.echo(json.dumps(boss_runs, indent=2))
        else:
            _print_runs_table(boss_runs)

    finally:
        await _cleanup_event_store()


def _print_runs_table(runs: list[dict]) -> None:
    if not runs:
        click.echo("No BOSS runs found.")
        return

    click.echo()
    click.echo(f"{'BOSS ID':<36} | {'Started':<20} | {'Events':<6} | Status")
    click.echo("-" * 80)
    for r in runs:
        click.echo(
            f"{r['boss_id']} | {r['started_at'][:19]} | {r['event_count']:<6} | {r['status']}"
        )
    click.echo()


if __name__ == "__main__":
    cli()
