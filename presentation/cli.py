"""Presentation Layer - CLI Interface.

This module provides the command-line interface for interacting with
the multi-agent system. It handles user input, displays output, and
coordinates the application layer.

Dependency: Presentation → Application ONLY

This follows strict layered architecture principles where:
- Presentation calls Application Services to execute use cases
- Presentation receives DTOs (Data Transfer Objects) from Application
- Presentation NEVER depends on Domain (aggregates, entities)
- Infrastructure is hidden behind Port interfaces

Reference: Martin Fowler - DTOs prevent domain aggregates from leaking
https://martinfowler.com/eaaCatalog/dataTransferObject.html
"""

import sys
from dataclasses import dataclass
from uuid import uuid4

from core.application.execution_service import AgentExecutionService


@dataclass
class CLIConfig:
    """Configuration for CLI behavior."""

    # Whether to show detailed progress output
    verbose: bool = True


class CLI:
    """Command-line interface for the Recursive Multi-Agent System.

    This class encapsulates all user-facing interactions:
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
        print('  python main.py "<your task description>"')
        print()
        print("Examples:")
        print('  python main.py "Build a REST API with authentication"')
        print('  python main.py "Analyze this codebase for security issues"')
        print('  python main.py "Refactor the payment module to use Strategy pattern"')
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

    async def _bootstrap_boss_agent(self, task_description: str) -> uuid4:
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

    async def _run_orchestration_loop(self, root_id: uuid4) -> None:
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

    async def _display_final_result(self, root_id: uuid4) -> None:
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

    async def run(self) -> None:
        """Main entry point for the CLI.

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

        # Display task
        self._print_banner()
        print(f"Task: {task_description}")
        print()

        try:
            # Step 2: Initialize infrastructure
            await self._initialize_infrastructure()

            # Step 3: Bootstrap BOSS agent
            root_id = await self._bootstrap_boss_agent(task_description)

            # Step 4: Run orchestration loop
            await self._run_orchestration_loop(root_id)

            # Step 5: Display final result
            await self._display_final_result(root_id)

        finally:
            # Step 6: Cleanup
            await self._cleanup()
