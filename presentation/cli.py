"""CLI Interface: run, events, summary, list commands via Click."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from uuid import UUID

import click

from core.domain.model import AgentRole
from presentation.context import EventStoreContext
from presentation.formatters import ProgressDisplayFormatter
from presentation.persistence import RunPersistence
from presentation.rendering import OutputRenderer


if TYPE_CHECKING:
    from core.application.execution_service import AgentExecutionService


# Factory function injected by bootstrap layer (avoids presentation→bootstrap dependency)
_bootstrap_factory: Callable[[Path | None], CLI] | None = None


def set_bootstrap_factory(factory: Callable[[Path | None], CLI]) -> None:
    """Set the bootstrap factory function.

    Called by bootstrap layer to inject the wiring function.
    This avoids presentation layer importing from bootstrap.
    """
    global _bootstrap_factory
    _bootstrap_factory = factory


@dataclass
class CLIConfig:
    """CLI configuration options."""

    verbose: bool = True
    output_directory: str = "./output"


class CLI:
    """Encapsulates task execution workflow: init -> run -> display -> cleanup.

    Single Responsibility: Orchestrates execution flow using injected services.
    """

    def __init__(
        self,
        execution_service: AgentExecutionService,
        config: CLIConfig | None = None,
    ) -> None:
        self.execution_service = execution_service
        self.config = config or CLIConfig()
        self._output_dir = Path(self.config.output_directory)
        self._persistence = RunPersistence(self._output_dir)
        self._renderer = OutputRenderer

    async def _initialize_infrastructure(self) -> None:
        """Initialize infrastructure adapters and connect to database."""
        if self.config.verbose:
            self._renderer.print_step(1, 5, "Initializing infrastructure adapters...")
            self._renderer.print_success("Event Store: PostgreSQL")
            self._renderer.print_success("LLM Adapter: LiteLLM")
            self._renderer.print_success("Worker Tools: Claude Code PTY, OpenHands (routed by config)")
            click.echo()

        if self.config.verbose:
            self._renderer.print_step(2, 5, "Connecting to PostgreSQL and initializing schema...")

        try:
            await self.execution_service.initialize()
            if self.config.verbose:
                self._renderer.print_success("Connected to PostgreSQL")
                self._renderer.print_success("Schema initialized")
                click.echo()
        except Exception as e:
            self._renderer.print_database_error(e)
            sys.exit(1)

    async def _bootstrap_boss_agent(self, task_description: str) -> UUID:
        """Create the root BOSS agent."""
        if self.config.verbose:
            self._renderer.print_step(3, 5, "Creating root BOSS agent...")

        try:
            root_id = await self.execution_service.create_boss_agent(
                task_description=task_description
            )
            if self.config.verbose:
                self._renderer.print_success(f"BOSS Agent Created (ID: {root_id})")
                self._renderer.print_success(f"Task Assigned: {task_description}")
                click.echo()
            return root_id
        except Exception as e:
            self._renderer.print_error(f"Failed to create BOSS agent: {e}")
            await self.execution_service.cleanup()
            sys.exit(1)

    async def _run_orchestration_loop(self, root_id: UUID) -> None:
        """Run the main orchestration loop."""
        if self.config.verbose:
            self._renderer.print_step(4, 5, "Starting orchestration loop...")
            self._renderer.print_info("(This may take a while depending on task complexity)")
            click.echo()

        try:
            await self.execution_service.run_system_loop(root_id)
            if self.config.verbose:
                self._renderer.print_success("All agents completed execution")
                click.echo()
        except Exception as e:
            self._renderer.print_error(f"Orchestration loop failed: {e}")
            await self.execution_service.cleanup()
            sys.exit(1)

    async def _display_final_result(self, root_id: UUID) -> None:
        """Display the final execution result."""
        if self.config.verbose:
            self._renderer.print_step(5, 5, "Fetching final result...")

        try:
            final_result = await self.execution_service.get_agent_result(root_id)
            stats = await self.execution_service.get_system_statistics()

            self._renderer.print_final_result(
                result=final_result.result,
                status=final_result.status,
                total_agents=stats.total_agents,
            )
        except Exception as e:
            self._renderer.print_error(f"Failed to fetch final result: {e}")

    async def _cleanup(self) -> None:
        """Clean up resources."""
        await self.execution_service.cleanup()
        if self.config.verbose:
            self._renderer.print_shutdown()

    async def run_with_task(self, task_description: str) -> None:
        """Execute a task from start to finish."""
        self._renderer.print_banner()
        click.echo(f"Task: {task_description}")
        click.echo()

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
                self._persistence.save_last_run(root_id, task_description, status)
            await self._cleanup()


# Click command group and context
class CLIContext:
    """Click context holder for CLI state."""

    def __init__(self, config_path: Path | None = None, output_dir: Path | None = None) -> None:
        self.config_path = config_path
        self.output_dir = output_dir or Path("./output")

    @property
    def persistence(self) -> RunPersistence:
        """Get run persistence for this context."""
        return RunPersistence(self.output_dir)


pass_cli_context = click.make_pass_decorator(CLIContext, ensure=True)


@click.group()
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, path_type=Path),
    help="Path to YAML configuration file",
)
@click.pass_context
def cli(ctx: click.Context, config: Path | None) -> None:
    """Arise Sec Lion CLI.

    A recursive, self-healing multi-agent orchestration platform.
    """
    output_dir = Path("./output")
    if config:
        from config import Settings

        settings = Settings.from_yaml(config)
        output_dir = Path(settings.output.directory)

    ctx.obj = CLIContext(config_path=config, output_dir=output_dir)


@cli.command()
@click.argument("task")
@pass_cli_context
def run(ctx: CLIContext, task: str) -> None:
    """Run a task with the multi-agent system.

    TASK is the description of what you want to accomplish.

    Examples:

        python main.py run "Build a REST API with authentication"

        python main.py run "Analyze this codebase for security issues"
    """
    if _bootstrap_factory is None:
        raise RuntimeError(
            "Bootstrap factory not configured. "
            "Ensure bootstrap layer has initialized before running CLI commands."
        )
    app = _bootstrap_factory(ctx.config_path)
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
@pass_cli_context
def events(
    ctx: CLIContext,
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
    asyncio.run(_show_events(ctx, agent_id, fmt, output, errors_only))


async def _show_events(
    ctx: CLIContext,
    agent_id: UUID | None,
    fmt: str,
    output: str | None,
    errors_only: bool,
) -> None:
    """Show events for an agent (async implementation)."""
    from core.query.projections import ProjectionPipelineBuilder

    async with EventStoreContext.from_config_path(ctx.config_path) as event_store:
        resolved_id = agent_id or ctx.persistence.get_last_run_id()
        if resolved_id is None:
            click.echo("Error: No agent-id specified and no last run found.")
            click.echo("Run a task first or specify --agent-id")
            return

        builder = ProjectionPipelineBuilder(event_store)
        if errors_only:
            builder = builder.with_filter("errors_only")
        builder = builder.with_formatter(fmt)

        await _execute_pipeline(builder, resolved_id, output)


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
@pass_cli_context
def summary(ctx: CLIContext, agent_id: UUID | None, fmt: str, output: str | None) -> None:
    """Show summary projection for a task run.

    Displays aggregated statistics including event counts,
    error count, and timeline information.

    Examples:

        python main.py summary

        python main.py summary --format text

        python main.py summary --output summary.json
    """
    asyncio.run(_show_summary(ctx, agent_id, fmt, output))


async def _show_summary(
    ctx: CLIContext,
    agent_id: UUID | None,
    fmt: str,
    output: str | None,
) -> None:
    """Show summary for an agent (async implementation)."""
    from core.query.projections import ProjectionPipelineBuilder

    async with EventStoreContext.from_config_path(ctx.config_path) as event_store:
        resolved_id = agent_id or ctx.persistence.get_last_run_id()
        if resolved_id is None:
            click.echo("Error: No agent-id specified and no last run found.")
            click.echo("Run a task first or specify --agent-id")
            return

        builder = ProjectionPipelineBuilder(event_store)
        builder = builder.with_output("summary")
        builder = builder.with_formatter(fmt)

        await _execute_pipeline(builder, resolved_id, output)


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
@pass_cli_context
def list_runs(ctx: CLIContext, limit: int, fmt: str) -> None:
    """List past BOSS agent runs.

    Shows recent task executions with their IDs and status.

    Examples:

        python main.py list

        python main.py list --limit 5

        python main.py list --format json
    """
    asyncio.run(_list_runs(ctx, limit, fmt))


async def _list_runs(ctx: CLIContext, limit: int, fmt: str) -> None:
    """List BOSS runs (async implementation).

    Uses get_all_events_grouped() for single-query efficiency (avoids N+1).
    """
    from core.domain.events import AgentCreated

    async with EventStoreContext.from_config_path(ctx.config_path) as event_store:
        all_events = await event_store.get_all_events_grouped()

        boss_runs = []
        for aid, events in all_events.items():
            if not events:
                continue

            first_event = events[0]
            if isinstance(first_event, AgentCreated) and first_event.role == AgentRole.BOSS.value:
                task = _extract_task_description(events)
                last_event = events[-1]

                boss_runs.append({
                    "boss_id": str(aid),
                    "task": task[:50] + "..." if len(task) > 50 else task,
                    "started_at": first_event.occurred_at.isoformat(),
                    "event_count": len(events),
                    "status": type(last_event).__name__,
                })

        boss_runs.sort(key=lambda x: x["started_at"], reverse=True)
        boss_runs = boss_runs[:limit]

        if fmt == "json":
            click.echo(json.dumps(boss_runs, indent=2))
        else:
            OutputRenderer.print_runs_table(boss_runs)


def _extract_task_description(events: list) -> str:
    """Extract task description from event list."""
    for event in events:
        if hasattr(event, "task_description"):
            return event.task_description
    return "N/A"


async def _execute_pipeline(
    builder: "ProjectionPipelineBuilder",
    agent_id: UUID,
    output: str | None,
) -> None:
    """Execute a projection pipeline with optional file output."""
    from core.query.projections import ProjectionPipelineBuilder

    builder = builder.to_sink("file", path=output) if output else builder.to_sink("stdout")
    pipeline = builder.build()
    await pipeline.execute(agent_id)

    if output:
        click.echo(f"Written to {output}")


# Convenience alias for progress callback
format_event_progress = ProgressDisplayFormatter.display


if __name__ == "__main__":
    cli()
