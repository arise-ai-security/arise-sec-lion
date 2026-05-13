"""CLI Interface: Pure CLI class for task execution.

This module contains the CLI class that orchestrates task execution.
It receives fully-wired dependencies via constructor injection from
the bootstrap layer. No Click decorators - command routing is done
in bootstrap/cli_main.py.

Architecture:
    bootstrap/cli_main.py (argparse + wiring) -> CLI class (pure, testable)
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from core.domain.events.events import RunCompleted
from presentation.persistence import RunPersistence
from presentation.rendering import OutputRenderer


if TYPE_CHECKING:
    from core.application.execution_service import AgentExecutionService
    from core.ports.event_store_port import EventStoreReadPort


RunStatus = Literal["success", "timeout", "failed"]


@dataclass(frozen=True)
class RunResult:
    """Presentation-level outcome returned by CLI.run_task.

    Distinguishes success, timeout, and failed paths so the bootstrap layer
    can persist an accurate run manifest without re-inferring state.
    """

    root_id: UUID
    status: RunStatus


@dataclass
class CLIConfig:
    """CLI configuration options."""

    verbose: bool = True
    output_directory: str = "./runs"
    default_worker_tool: str = "claude_code"


class CLI:
    """Orchestrates task execution workflow: init -> run -> display -> cleanup.

    Single Responsibility: Orchestrates execution flow using injected services.
    This class is pure - it receives all dependencies via constructor injection
    and contains no command-line parsing logic.
    """

    def __init__(
        self,
        execution_service: "AgentExecutionService",
        event_store: "EventStoreReadPort",
        config: CLIConfig | None = None,
    ) -> None:
        self.execution_service = execution_service
        self._event_store = event_store
        self.config = config or CLIConfig()
        self._output_dir = Path(self.config.output_directory)
        self._persistence = RunPersistence(self._output_dir)
        self._renderer = OutputRenderer

    async def _initialize_infrastructure(self) -> None:
        if self.config.verbose:
            self._renderer.print_step(1, 5, "Initializing infrastructure adapters...")
            self._renderer.print_success("Event Store: PostgreSQL")
            self._renderer.print_success("LLM Adapter: LiteLLM")
            worker_display = {
                "claude_code": "Claude Code (Agent SDK)",
                "openhands": "OpenHands",
                "google_adk": "Google ADK (Gemini)",
            }.get(self.config.default_worker_tool, self.config.default_worker_tool)
            self._renderer.print_success(f"Worker Tool: {worker_display}")
            print()

        if self.config.verbose:
            self._renderer.print_step(2, 5, "Connecting to PostgreSQL and initializing schema...")

        try:
            await self.execution_service.initialize()
            if self.config.verbose:
                self._renderer.print_success("Connected to PostgreSQL")
                self._renderer.print_success("Schema initialized")
                print()
        except Exception as e:
            self._renderer.print_database_error(e)
            sys.exit(1)

    async def _bootstrap_boss_agent(
        self,
        task_description: str,
        domain_context: object | None = None,
    ) -> UUID:
        if self.config.verbose:
            self._renderer.print_step(3, 5, "Creating root BOSS agent...")

        try:
            root_id = await self.execution_service.create_boss_agent(
                task_description=task_description,
                domain_context=domain_context,
            )
            if self.config.verbose:
                self._renderer.print_success(f"BOSS Agent Created (ID: {root_id})")
                self._renderer.print_success(f"Task Assigned: {task_description}")
                print()
            return root_id
        except Exception as e:
            self._renderer.print_error(f"Failed to create BOSS agent: {e}")
            await self.execution_service.cleanup()
            sys.exit(1)

    async def _run_orchestration_loop(self, root_id: UUID) -> None:
        if self.config.verbose:
            self._renderer.print_step(4, 5, "Starting orchestration loop...")
            self._renderer.print_info("(This may take a while depending on task complexity)")
            print()

        await self.execution_service.run_system_loop(root_id)
        if self.config.verbose:
            self._renderer.print_success("All agents completed execution")
            print()

    async def _display_final_result(self, root_id: UUID) -> None:
        if self.config.verbose:
            self._renderer.print_step(5, 5, "Fetching final result...")

        try:
            final_result = await self.execution_service.get_agent_result(root_id)
            # Use hierarchy-specific stats to count only agents from this run
            stats = await self.execution_service.get_system_statistics(root_id=root_id)

            self._renderer.print_final_result(
                result=final_result.result,
                status=final_result.status,
                total_agents=stats.total_agents,
            )
        except Exception as e:
            self._renderer.print_error(f"Failed to fetch final result: {e}")

    async def _determine_run_status(self, root_id: UUID) -> RunStatus:
        """Classify the run outcome from the latest RunCompleted event.

        Returns "success" for a clean finish, "timeout" when the orchestration
        hit its hard deadline, and "failed" in all other cases (including the
        absence of a RunCompleted event).
        """
        events = await self._event_store.get_events(root_id)
        for event in reversed(events):
            if isinstance(event, RunCompleted):
                if event.status == "timed_out":
                    return "timeout"
                if event.status == "completed":
                    return "success"
                return "failed"
        return "failed"

    async def _cleanup(self) -> None:
        """Clean up resources."""
        await self.execution_service.cleanup()
        if self.config.verbose:
            self._renderer.print_shutdown()

    async def run_task(
        self,
        task_description: str,
        domain_context: object | None = None,
    ) -> RunResult:
        """Execute a task from start to finish and report the outcome.

        Returns a RunResult so callers can distinguish success, timeout, and
        failed runs without re-inferring state from events or agent status.
        """
        self._renderer.print_banner()
        print(f"Task: {task_description}")
        print()

        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Infrastructure init and BOSS creation are preconditions for producing
        # a RunResult. If either fails, the legacy sys.exit paths remain — no
        # partial RunResult is returned for catastrophic pre-run failures.
        await self._initialize_infrastructure()
        try:
            root_id = await self._bootstrap_boss_agent(task_description, domain_context)
        except BaseException:
            await self._cleanup()
            raise

        status: RunStatus = "failed"
        try:
            await self._run_orchestration_loop(root_id)
            status = await self._determine_run_status(root_id)
            await self._display_final_result(root_id)
        except Exception as e:
            status = "failed"
            self._renderer.print_error(f"Orchestration loop failed: {e}")
        finally:
            self._persistence.save_last_run(root_id, task_description, status)
            self._persistence.maybe_write_run_result(root_id, status)
            await self._cleanup()

        return RunResult(root_id=root_id, status=status)
