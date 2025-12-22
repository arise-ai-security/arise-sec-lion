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
    from core.ports.event_store_port import EventStorePort
    from core.query.projections import ProjectionPipelineBuilder


@dataclass
class CLIConfig:
    verbose: bool = True
    output_directory: str = "./output"


def format_status_display(agents_status: list) -> None:
    """Print current status of all agents (pending, active, etc.)."""
    if not agents_status:
        return

    # Group agents by status
    pending_agents = [a for a in agents_status if a["status"] == "pending"]
    analyzing_agents = [a for a in agents_status if a["status"] == "analyzing"]
    in_progress_agents = [a for a in agents_status if a["status"] == "in_progress"]
    waiting_agents = [a for a in agents_status if a["status"] == "waiting"]

    # Only display if there are pending agents
    if pending_agents or analyzing_agents or waiting_agents:
        print()
        print("   ┌─────────────────────────────────────────────────────────────────┐")

        # Show pending agents
        if pending_agents:
            print(f"    📋 Pending: {len(pending_agents)} agent(s)")
            for agent in pending_agents[:3]:  # Show first 3
                budget_str = f"${agent['budget']:.0f}" if agent['budget'] > 0 else ""
                print(f"       • [{agent['agent_id']}] {agent['role']} {budget_str}")
            if len(pending_agents) > 3:
                print(f"       ... and {len(pending_agents) - 3} more")

        # Show analyzing agents
        if analyzing_agents:
            print(f"    🔍 Analyzing: {len(analyzing_agents)} agent(s)")
            for agent in analyzing_agents[:3]:
                budget_str = f"${agent['budget']:.0f}" if agent['budget'] > 0 else ""
                print(f"       • [{agent['agent_id']}] {agent['role']} {budget_str}")

        # Show waiting agents
        if waiting_agents:
            print(f"    ⏳ Waiting: {len(waiting_agents)} agent(s)")

        # Show in-progress agents
        if in_progress_agents:
            print(f"    ⚡ Working: {len(in_progress_agents)} agent(s)")

        # Calculate total budget
        total_budget = sum(a["budget"] for a in agents_status)
        print(f"    💰 Total Budget: ${total_budget:.0f}")

        print("   └─────────────────────────────────────────────────────────────────┘")
        print()


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
        case "BudgetAllocated":
            amount = getattr(event, "amount", 0)
            source = getattr(event, "source", "unknown")
            print(f"   💰 [{agent_id}] Budget allocated: {amount:.1f} (source: {source})")
        case "BudgetAdjusted":
            adjustment = getattr(event, "adjustment", 0)
            new_balance = getattr(event, "new_balance", 0)
            reason = getattr(event, "reason", "")[:40]
            sign = "+" if adjustment >= 0 else ""
            print(f"   💸 [{agent_id}] Budget adjusted: {sign}{adjustment:.1f} → {new_balance:.1f} ({reason})")
        case "BudgetRecollected":
            child_id = str(getattr(event, "child_id", "?"))[:8]
            amount = getattr(event, "amount_recollected", 0)
            succeeded = getattr(event, "child_succeeded", False)
            status = "✓" if succeeded else "✗"
            print(f"   🔄 [{agent_id}] {status} Recollected {amount:.1f} from child {child_id}...")
        case "TaskEnqueued":
            subtask = getattr(event, "subtask", None)
            desc = getattr(subtask, "description", "")[:40] if subtask else ""
            print(f"   📥 [{agent_id}] Task enqueued: {desc}...")
        case "TaskDequeued":
            subtask = getattr(event, "subtask", None)
            desc = getattr(subtask, "description", "")[:40] if subtask else ""
            print(f"   📤 [{agent_id}] Task dequeued: {desc}...")
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
        print("║  Recursive Multi-Agent System - Powered by Event Sourcing         ║")
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
        print("  python main.py report    - Show comprehensive work report")
        print("  python main.py list      - List past BOSS agent runs")
        print("  python main.py budget    - Show agent budget information")
        print("  python main.py tasks     - Show agent task queues")
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

            stats = await self.execution_service.get_system_statistics(root_id)
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
        settings = Settings.from_yaml(config_path) if config_path else Settings.load()
        connection_string = settings.database.connection_string
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
    """Arise Sec Lion CLI.

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
@click.option(
    "--worker-shortcut-prob",
    type=float,
    default=None,
    help="Probability (0.0-1.0) that pending agents become workers directly (default: 0.3)",
)
@click.option(
    "--budget-threshold",
    type=float,
    default=None,
    help="Budget threshold (0.0-1.0) below which pending agents become workers (default: 0.02)",
)
@click.pass_context
def run(
    ctx: click.Context,
    task: str,
    worker_shortcut_prob: float | None,
    budget_threshold: float | None,
) -> None:
    """Run a task with the multi-agent system.

    TASK is the description of what you want to accomplish.

    Examples:

        python main.py run "Build a REST API with authentication"

        python main.py run "Analyze this codebase for security issues"

        python main.py run --worker-shortcut-prob 0.5 "Quick prototype"

        python main.py run --budget-threshold 0.05 "Complex task"
    """
    from bootstrap import bootstrap

    config_path = ctx.obj.get("config_path")
    app = bootstrap(
        config_path=config_path,
        worker_shortcut_probability=worker_shortcut_prob,
        budget_threshold_ratio=budget_threshold,
    )

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
    from core.query.projections import ProjectionPipelineBuilder

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
    from core.query.projections import ProjectionPipelineBuilder

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
            if isinstance(first_event, AgentCreated) and first_event.role == "boss":
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
    default="text",
    help="Output format",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path",
)
def budget(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    """Show budget information for agents in a task run.

    Displays budget allocation, adjustments, and current balance
    for each agent in the hierarchy.

    Examples:

        python main.py budget

        python main.py budget --format json

        python main.py budget --agent-id <uuid>
    """
    asyncio.run(_show_budget(agent_id, fmt, output))


async def _show_budget(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    from core.domain.events import (
        AgentCreated,
        BudgetAdjusted,
        BudgetAllocated,
        BudgetRecollected,
        TaskAssigned,
    )
    from core.query.projections.hierarchy_collector import HierarchyCollector

    try:
        event_store = await _get_event_store()
        output_dir = _get_output_dir()

        if agent_id is None:
            agent_id = get_last_run_id(output_dir)
            if agent_id is None:
                click.echo("Error: No agent-id specified and no last run found.")
                click.echo("Run a task first or specify --agent-id")
                return

        # Collect all agent IDs in the hierarchy
        collector = HierarchyCollector(event_store)
        all_ids = await collector.collect(agent_id)

        budget_data: list[dict] = []

        for aid in all_ids:
            events = await event_store.get_events(aid)

            agent_info: dict = {
                "agent_id": str(aid),
                "role": "unknown",
                "task": "",
                "initial_budget": 0.0,
                "current_budget": 0.0,
                "adjustments": [],
                "recollections": [],
            }

            current_budget = 0.0

            for event in events:
                if isinstance(event, AgentCreated):
                    agent_info["role"] = event.role
                elif isinstance(event, TaskAssigned):
                    task = event.task_description
                    agent_info["task"] = task[:50] + "..." if len(task) > 50 else task
                elif isinstance(event, BudgetAllocated):
                    agent_info["initial_budget"] = event.amount
                    current_budget = event.amount
                elif isinstance(event, BudgetAdjusted):
                    agent_info["adjustments"].append({
                        "amount": event.adjustment,
                        "reason": event.reason,
                        "new_balance": event.new_balance,
                    })
                    current_budget = event.new_balance
                elif isinstance(event, BudgetRecollected):
                    agent_info["recollections"].append({
                        "child_id": str(event.child_id),
                        "original_allocation": event.original_allocation,
                        "remaining": event.remaining_budget,
                        "recollected": event.amount_recollected,
                        "succeeded": event.child_succeeded,
                    })
                    current_budget += event.amount_recollected

            agent_info["current_budget"] = current_budget
            budget_data.append(agent_info)

        if output:
            with open(output, "w") as f:
                f.write(json.dumps(budget_data, indent=2))
            click.echo(f"Written to {output}")
        elif fmt == "json":
            click.echo(json.dumps(budget_data, indent=2))
        else:
            _print_budget_table(budget_data)

    finally:
        await _cleanup_event_store()


def _print_budget_table(budget_data: list[dict]) -> None:
    if not budget_data:
        click.echo("No budget information found.")
        return

    click.echo()
    click.echo("╔═══════════════════════════════════════════════════════════════════╗")
    click.echo("║  AGENT BUDGET SUMMARY                                             ║")
    click.echo("╚═══════════════════════════════════════════════════════════════════╝")
    click.echo()
    click.echo(f"{'Agent ID':<10} | {'Role':<8} | {'Initial':<10} | {'Current':<10} | Task")
    click.echo("-" * 80)

    for agent in budget_data:
        aid = agent["agent_id"][:8] + "..."
        role = agent["role"][:8]
        initial = f"{agent['initial_budget']:.1f}"
        current = f"{agent['current_budget']:.1f}"
        task = agent["task"][:30] if agent["task"] else "(no task)"
        click.echo(f"{aid:<10} | {role:<8} | {initial:<10} | {current:<10} | {task}")

        # Show adjustments if any
        for adj in agent.get("adjustments", []):
            sign = "+" if adj["amount"] >= 0 else ""
            click.echo(f"           └─ {sign}{adj['amount']:.1f}: {adj['reason'][:40]}")

        # Show recollections if any
        for rec in agent.get("recollections", []):
            status = "✓" if rec["succeeded"] else "✗"
            click.echo(
                f"           └─ {status} recollected {rec['recollected']:.1f} "
                f"from child {rec['child_id'][:8]}..."
            )

    click.echo()


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
    default="text",
    help="Output format",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path",
)
def tasks(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    """Show task queue information for agents in a task run.

    Displays task queue state including enqueued and dequeued tasks
    for each agent in the hierarchy.

    Examples:

        python main.py tasks

        python main.py tasks --format json

        python main.py tasks --agent-id <uuid>
    """
    asyncio.run(_show_tasks(agent_id, fmt, output))


async def _show_tasks(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    from core.domain.events import (
        AgentCreated,
        TaskAssigned,
        TaskDequeued,
        TaskEnqueued,
        TaskReinjected,
    )
    from core.query.projections.hierarchy_collector import HierarchyCollector

    try:
        event_store = await _get_event_store()
        output_dir = _get_output_dir()

        if agent_id is None:
            agent_id = get_last_run_id(output_dir)
            if agent_id is None:
                click.echo("Error: No agent-id specified and no last run found.")
                click.echo("Run a task first or specify --agent-id")
                return

        # Collect all agent IDs in the hierarchy
        collector = HierarchyCollector(event_store)
        all_ids = await collector.collect(agent_id)

        tasks_data: list[dict] = []

        for aid in all_ids:
            events = await event_store.get_events(aid)

            agent_info: dict = {
                "agent_id": str(aid),
                "role": "unknown",
                "main_task": "",
                "queue_history": [],
                "current_queue": [],
            }

            # Track queue state
            current_queue: list[str] = []

            for event in events:
                if isinstance(event, AgentCreated):
                    agent_info["role"] = event.role
                elif isinstance(event, TaskAssigned):
                    task = event.task_description
                    agent_info["main_task"] = task[:80] + "..." if len(task) > 80 else task
                elif isinstance(event, TaskEnqueued):
                    desc = event.subtask.description
                    current_queue.append(desc)
                    agent_info["queue_history"].append({
                        "action": "enqueued",
                        "task": desc[:60] + "..." if len(desc) > 60 else desc,
                    })
                elif isinstance(event, TaskDequeued):
                    desc = event.subtask.description
                    if desc in current_queue:
                        current_queue.remove(desc)
                    agent_info["queue_history"].append({
                        "action": "dequeued",
                        "task": desc[:60] + "..." if len(desc) > 60 else desc,
                    })
                elif isinstance(event, TaskReinjected):
                    desc = event.subtask.description
                    current_queue.insert(0, desc)  # Reinjected at front
                    agent_info["queue_history"].append({
                        "action": "reinjected",
                        "task": desc[:60] + "..." if len(desc) > 60 else desc,
                        "retry_count": event.retry_count,
                    })

            agent_info["current_queue"] = [
                d[:60] + "..." if len(d) > 60 else d for d in current_queue
            ]
            agent_info["queue_size"] = len(current_queue)

            tasks_data.append(agent_info)

        if output:
            with open(output, "w") as f:
                f.write(json.dumps(tasks_data, indent=2))
            click.echo(f"Written to {output}")
        elif fmt == "json":
            click.echo(json.dumps(tasks_data, indent=2))
        else:
            _print_tasks_table(tasks_data)

    finally:
        await _cleanup_event_store()


def _print_tasks_table(tasks_data: list[dict]) -> None:
    if not tasks_data:
        click.echo("No task queue information found.")
        return

    click.echo()
    click.echo("╔═══════════════════════════════════════════════════════════════════╗")
    click.echo("║  AGENT TASK QUEUES                                                ║")
    click.echo("╚═══════════════════════════════════════════════════════════════════╝")
    click.echo()

    for agent in tasks_data:
        aid = agent["agent_id"][:8] + "..."
        role = agent["role"]
        main_task = agent["main_task"][:50] if agent["main_task"] else "(no task)"
        queue_size = agent["queue_size"]

        click.echo(f"🤖 Agent {aid} ({role})")
        click.echo(f"   Main Task: {main_task}")
        click.echo(f"   Queue Size: {queue_size}")

        # Show current queue
        if agent["current_queue"]:
            click.echo("   Current Queue:")
            for i, task in enumerate(agent["current_queue"], 1):
                click.echo(f"      {i}. {task}")

        # Show queue history
        if agent["queue_history"]:
            click.echo("   History:")
            for entry in agent["queue_history"][-5:]:  # Show last 5 entries
                action = entry["action"]
                task = entry["task"]
                if action == "enqueued":
                    click.echo(f"      + {task}")
                elif action == "dequeued":
                    click.echo(f"      - {task}")
                elif action == "reinjected":
                    retry = entry.get("retry_count", 1)
                    click.echo(f"      ↺ {task} (retry #{retry})")

        click.echo()


@cli.command("report")
@click.option(
    "--agent-id",
    type=click.UUID,
    help="Agent UUID (default: last run)",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "text"]),
    default="text",
    help="Output format",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path",
)
def report(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    """Show comprehensive work report for a task run.

    Displays a complete summary of all work done by the BOSS agent
    and its subtree, including worker reports and justifications.

    Examples:

        python main.py report

        python main.py report --format json

        python main.py report --output report.md
    """
    asyncio.run(_show_report(agent_id, fmt, output))


async def _show_report(agent_id: UUID | None, fmt: str, output: str | None) -> None:
    from core.query.projections.tree_summary import (
        TreeSummaryGenerator,
        format_tree_summary_json,
        format_tree_summary_text,
    )

    try:
        event_store = await _get_event_store()
        output_dir = _get_output_dir()

        if agent_id is None:
            agent_id = get_last_run_id(output_dir)
            if agent_id is None:
                click.echo("Error: No agent-id specified and no last run found.")
                click.echo("Run a task first or specify --agent-id")
                return

        generator = TreeSummaryGenerator(event_store)
        summary = await generator.generate(agent_id)

        if fmt == "json":
            import json as json_module
            result = json_module.dumps(format_tree_summary_json(summary), indent=2)
        else:
            result = format_tree_summary_text(summary)

        if output:
            Path(output).write_text(result)
            click.echo(f"Report written to {output}")
        else:
            click.echo(result)

    finally:
        await _cleanup_event_store()


if __name__ == "__main__":
    cli()
