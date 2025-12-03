"""Presentation Layer - CLI Interface.

This module provides the command-line interface for interacting with
the multi-agent system using Click for subcommand support.

Commands:
    run <task>     - Execute a task with the multi-agent system
    events         - View events for an agent run
    summary        - Show summary projection for a run
    list           - List past BOSS agent runs

Dependency: Presentation → Application ONLY

This follows strict layered architecture principles where:
- Presentation calls Application Services to execute use cases
- Presentation receives DTOs (Data Transfer Objects) from Application
- Presentation NEVER depends on Domain (aggregates, entities)
- Infrastructure is hidden behind Port interfaces

Reference: Martin Fowler - DTOs prevent domain aggregates from leaking
https://martinfowler.com/eaaCatalog/dataTransferObject.html
"""

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


# ==============================================================================
# Configuration
# ==============================================================================


@dataclass
class CLIConfig:
    """Configuration for CLI behavior."""

    verbose: bool = True
    output_directory: str = "./output"


# ==============================================================================
# Last Run Tracking
# ==============================================================================


def get_last_run(output_dir: Path) -> dict | None:
    """Load the last run info from .last_run.json.

    Args:
        output_dir: Directory containing .last_run.json.

    Returns:
        Dict with boss_id, task, started_at, etc., or None if not found.
    """
    path = output_dir / ".last_run.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def save_last_run(output_dir: Path, boss_id: UUID, task: str, status: str) -> None:
    """Save the last run info to .last_run.json.

    Args:
        output_dir: Directory to save .last_run.json.
        boss_id: UUID of the BOSS agent.
        task: Task description.
        status: Final status (completed, failed, etc.).
    """
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
    """Get the BOSS agent ID from the last run.

    Args:
        output_dir: Directory containing .last_run.json.

    Returns:
        UUID of the last BOSS agent, or None if not found.
    """
    last_run = get_last_run(output_dir)
    if last_run and "boss_id" in last_run:
        return UUID(last_run["boss_id"])
    return None


# ==============================================================================
# CLI Class (preserved for run command and tests)
# ==============================================================================


class CLI:
    """Command-line interface for the Recursive Multi-Agent System.

    This class encapsulates the task execution workflow:
    - Parsing command line arguments
    - Displaying progress and results
    - Error handling and user feedback

    Architecture Note:
        The CLI depends ONLY on the Application layer (execution service).
        It does NOT depend on Bootstrap (that would be backwards!).
        All infrastructure interactions are handled through application service.
    """

    def __init__(
        self,
        execution_service: AgentExecutionService,
        config: CLIConfig | None = None,
    ) -> None:
        """Initialize the CLI.

        Args:
            execution_service: Application service for agent execution.
            config: CLI configuration. Uses defaults if not provided.
        """
        self.execution_service = execution_service
        self.config = config or CLIConfig()
        self._output_dir = Path(self.config.output_directory)

    def _print_banner(self) -> None:
        """Display the application banner."""
        print("╔═══════════════════════════════════════════════════════════════════╗")
        print("║  Recursive Multi-Agent System - Powered by Event Sourcing       ║")
        print("╚═══════════════════════════════════════════════════════════════════╝")
        print()

    def _print_usage(self) -> None:
        """Display usage instructions."""
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
        """Parse command line arguments and return task description.

        Returns:
            Task description string, or None if no task provided.
        """
        if len(sys.argv) < 2:
            return None

        return " ".join(sys.argv[1:])

    async def _initialize_infrastructure(self) -> None:
        """Initialize infrastructure via application service."""
        if self.config.verbose:
            print("[1/5] Initializing infrastructure adapters...")
            print("   ✓ Event Store: PostgreSQL")
            print("   ✓ LLM Adapter: LiteLLM")
            print("   ✓ Worker Tool: Claude Code PTY")
            print()

        if self.config.verbose:
            print("[2/5] Connecting to PostgreSQL and initializing schema...")

        try:
            # Delegate infrastructure lifecycle to application service
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
        """Create and persist the root BOSS agent via application service.

        Args:
            task_description: The task to assign to the BOSS.

        Returns:
            UUID of the created BOSS agent.
        """
        if self.config.verbose:
            print("[3/5] Creating root BOSS agent...")

        try:
            # Delegate BOSS creation to application service
            # Model is selected based on role (configured in settings)
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
        """Execute the multi-agent orchestration loop via application service.

        Args:
            root_id: UUID of the root BOSS agent.
        """
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
        """Load and display the final result via application service.

        Args:
            root_id: UUID of the root BOSS agent.
        """
        if self.config.verbose:
            print("[5/5] Fetching final result...")

        try:
            # Query final agent state via application service
            # Returns DTO (not domain aggregate) - strict layered architecture
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

            # Show summary statistics via application service
            stats = await self.execution_service.get_system_statistics()
            print(f"Total Agents Created: {stats.total_agents}")
            print()

        except Exception as e:
            print(f"   ✗ Failed to fetch final result: {e}")

    async def _cleanup(self) -> None:
        """Cleanup resources via application service."""
        await self.execution_service.cleanup()
        if self.config.verbose:
            print("✓ System shutdown complete")

    async def run_with_task(self, task_description: str) -> None:
        """Run the CLI with a specific task description.

        This method is called by Click commands to execute tasks.

        Args:
            task_description: The task to execute.
        """
        # Display task
        self._print_banner()
        print(f"Task: {task_description}")
        print()

        # Ensure output directory exists
        self._output_dir.mkdir(parents=True, exist_ok=True)

        status = "failed"
        root_id = None

        try:
            # Step 1: Initialize infrastructure
            await self._initialize_infrastructure()

            # Step 2: Bootstrap BOSS agent
            root_id = await self._bootstrap_boss_agent(task_description)

            # Step 3: Run orchestration loop
            await self._run_orchestration_loop(root_id)

            # Step 4: Display final result
            await self._display_final_result(root_id)

            status = "completed"

        finally:
            # Save last run info
            if root_id:
                save_last_run(self._output_dir, root_id, task_description, status)

            # Step 5: Cleanup
            await self._cleanup()

    async def run(self) -> None:
        """Main entry point for the CLI (legacy mode).

        This method orchestrates the entire application lifecycle:
        1. Parse user input
        2. Initialize infrastructure
        3. Bootstrap BOSS agent
        4. Run orchestration loop
        5. Display results
        6. Cleanup
        """
        # Step 1: Parse command line arguments
        task_description = self._parse_arguments()

        if task_description is None:
            self._print_usage()
            sys.exit(1)

        await self.run_with_task(task_description)


# ==============================================================================
# Click CLI Commands
# ==============================================================================


# Store for lazy-loaded resources
_cli_context: dict = {}


def _get_output_dir() -> Path:
    """Get the output directory from context or default."""
    return Path(_cli_context.get("output_directory", "./output"))


async def _get_event_store() -> EventStorePort:
    """Get or create the event store connection.

    Returns:
        Connected EventStorePort instance.
    """
    if "event_store" not in _cli_context:
        from infrastructure.adapters.postgres_event_store import PostgresEventStore

        config_path = _cli_context.get("config_path")
        if config_path:
            from config import Settings

            settings = Settings.from_yaml(config_path)
            connection_string = settings.infrastructure.postgres_connection_string
        else:
            connection_string = "postgresql://arise:arise@localhost:5432/arise_events"

        event_store = PostgresEventStore(connection_string)
        await event_store.connect()
        _cli_context["event_store"] = event_store

    return _cli_context["event_store"]


async def _cleanup_event_store() -> None:
    """Cleanup the event store connection if open."""
    if "event_store" in _cli_context:
        await _cli_context["event_store"].disconnect()
        del _cli_context["event_store"]


async def _execute_pipeline(
    builder: ProjectionPipelineBuilder,
    agent_id: UUID,
    output: str | None,
) -> None:
    """Execute a projection pipeline with common finalization logic.

    This helper reduces duplication between events and summary commands.

    Args:
        builder: Pre-configured pipeline builder.
        agent_id: Target agent UUID.
        output: Output file path, or None for stdout.
    """
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

    # Load output directory from config if available
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
    """Query events using projection pipeline."""
    # Import here to register sinks
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

        # Build pipeline with existing projection system
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
    """Query summary using projection pipeline."""
    # Import here to register sinks
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

        # Build pipeline for summary output
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
    """List BOSS runs from event store."""
    from core.domain.events import AgentCreated

    try:
        event_store = await _get_event_store()

        # Get all aggregate IDs and filter for BOSS agents
        all_ids = await event_store.get_all_aggregate_ids()

        boss_runs = []
        for agent_id in all_ids:
            events = await event_store.get_events(agent_id)
            if not events:
                continue

            # Check if first event is AgentCreated with BOSS role
            first_event = events[0]
            if isinstance(first_event, AgentCreated) and first_event.role.value == "BOSS":
                # Get task description from TaskAssigned event if present
                task = "N/A"
                for event in events:
                    if hasattr(event, "task_description"):
                        task = event.task_description
                        break

                # Determine status from last event
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

        # Sort by started_at descending and limit
        boss_runs.sort(key=lambda x: x["started_at"], reverse=True)
        boss_runs = boss_runs[:limit]

        if fmt == "json":
            click.echo(json.dumps(boss_runs, indent=2))
        else:
            _print_runs_table(boss_runs)

    finally:
        await _cleanup_event_store()


def _print_runs_table(runs: list[dict]) -> None:
    """Format runs as a table."""
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


# Entry point for `python -m presentation.cli`
if __name__ == "__main__":
    cli()
