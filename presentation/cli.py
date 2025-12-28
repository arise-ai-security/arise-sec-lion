"""CLI Interface: Pure CLI class for task execution.

This module contains the CLI class that orchestrates task execution.
It receives fully-wired dependencies via constructor injection from
the bootstrap layer. No Click decorators - command routing is done
in bootstrap/cli_main.py.

Architecture:
    bootstrap/cli_main.py (argparse + wiring) -> CLI class (pure, testable)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from presentation.persistence import RunPersistence
from presentation.rendering import OutputRenderer


if TYPE_CHECKING:
    from core.application.execution_service import AgentExecutionService


@dataclass
class CLIConfig:
    """CLI configuration options."""

    verbose: bool = True
    output_directory: str = "./output"
    default_worker_tool: str = "claude_code"


class CLI:
    """Orchestrates task execution workflow: init -> run -> display -> cleanup.

    Single Responsibility: Orchestrates execution flow using injected services.
    This class is pure - it receives all dependencies via constructor injection
    and contains no command-line parsing logic.
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
                print()
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
            print()

        try:
            await self.execution_service.run_system_loop(root_id)
            if self.config.verbose:
                self._renderer.print_success("All agents completed execution")
                print()
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

    async def run_task(self, task_description: str) -> None:
        """Execute a task from start to finish.

        This is the main entry point for task execution. It:
        1. Initializes infrastructure
        2. Creates a BOSS agent
        3. Runs the orchestration loop
        4. Displays results
        5. Cleans up resources
        """
        self._renderer.print_banner()
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
                self._persistence.save_last_run(root_id, task_description, status)
            await self._cleanup()
