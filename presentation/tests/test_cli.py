"""Integration tests for CLI presentation layer.

These tests verify CLI behavior with mocked application services.
The CLI should correctly:
- Display appropriate output
- Delegate to execution service
- Handle errors gracefully
"""

from io import StringIO
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from core.domain.events.events import RunCompleted
from presentation.cli import CLI, CLIConfig, RunResult

from .conftest import FakeEventStore, FakeExecutionService


class TestCLIBanner:
    """Tests for CLI banner display."""

    def test_print_banner_outputs_header(self, cli_with_fake_service: CLI) -> None:
        """Test that banner prints the application header."""
        captured = StringIO()
        with patch("click.echo", side_effect=lambda x="": captured.write(x + "\n")):
            cli_with_fake_service._renderer.print_banner()

        output = captured.getvalue()
        assert "Recursive Multi-Agent System" in output
        assert "Event Sourcing" in output


class TestCLIInfrastructureInitialization:
    """Tests for CLI infrastructure initialization."""

    @pytest.mark.asyncio
    async def test_initialize_calls_service(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that initialization delegates to execution service."""
        await cli_with_fake_service._initialize_infrastructure()
        assert fake_execution_service.initialized is True

    @pytest.mark.asyncio
    async def test_initialize_verbose_outputs_progress(
        self,
        verbose_cli_with_fake_service: CLI,
    ) -> None:
        """Test that verbose mode outputs progress messages."""
        captured = StringIO()
        with patch("click.echo", side_effect=lambda x="": captured.write(x + "\n")):
            await verbose_cli_with_fake_service._initialize_infrastructure()

        output = captured.getvalue()
        assert "Initializing" in output or "infrastructure" in output.lower()


class TestCLIBossAgentCreation:
    """Tests for BOSS agent creation via CLI."""

    @pytest.mark.asyncio
    async def test_bootstrap_boss_creates_agent(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that BOSS creation delegates to execution service."""
        task = "Build a REST API"
        await cli_with_fake_service._bootstrap_boss_agent(task)

        assert fake_execution_service.last_task_description == task
        assert fake_execution_service.created_boss_id is not None

    @pytest.mark.asyncio
    async def test_bootstrap_boss_returns_id(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that BOSS creation returns the agent ID."""
        result = await cli_with_fake_service._bootstrap_boss_agent("Test task")
        assert result == fake_execution_service.created_boss_id


class TestCLIOrchestrationLoop:
    """Tests for orchestration loop via CLI."""

    @pytest.mark.asyncio
    async def test_run_orchestration_calls_service(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that orchestration loop delegates to execution service."""
        fake_execution_service.created_boss_id = "test-id"
        await cli_with_fake_service._run_orchestration_loop("test-id")

        assert fake_execution_service.system_loop_called is True


class TestCLIResultDisplay:
    """Tests for result display via CLI."""

    @pytest.mark.asyncio
    async def test_display_result_shows_status(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that final result displays status."""
        captured = StringIO()
        with patch("click.echo", side_effect=lambda x="": captured.write(x + "\n")):
            await cli_with_fake_service._display_final_result("test-id")

        output = captured.getvalue()
        assert "completed" in output.lower() or "FINAL RESULT" in output

    @pytest.mark.asyncio
    async def test_display_result_shows_statistics(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that final result displays agent statistics."""
        captured = StringIO()
        with patch("click.echo", side_effect=lambda x="": captured.write(x + "\n")):
            await cli_with_fake_service._display_final_result("test-id")

        output = captured.getvalue()
        assert "Total Agents" in output or "5" in output  # 5 from fake stats


class TestCLICleanup:
    """Tests for CLI cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_calls_service(
        self,
        cli_with_fake_service: CLI,
        fake_execution_service: FakeExecutionService,
    ) -> None:
        """Test that cleanup delegates to execution service."""
        await cli_with_fake_service._cleanup()
        assert fake_execution_service.cleaned_up is True


class TestCLIConfig:
    """Tests for CLI configuration."""

    def test_default_config_is_verbose(self) -> None:
        """Test that default CLI config has verbose=True."""
        config = CLIConfig()
        assert config.verbose is True

    def test_config_can_disable_verbose(self) -> None:
        """Test that verbose can be disabled."""
        config = CLIConfig(verbose=False)
        assert config.verbose is False

    def test_config_default_output_directory(self) -> None:
        """Test that CLIConfig has default output_directory."""
        config = CLIConfig()
        assert config.output_directory == "./runs"

    def test_config_custom_output_directory(self) -> None:
        """Test that CLIConfig accepts custom output_directory."""
        config = CLIConfig(output_directory="/custom/path")
        assert config.output_directory == "/custom/path"


class TestCLIRunTaskStatusMapping:
    """CLI.run_task() must map execution outcomes to the RunResult status.

    The contract: the orchestration loop emits RunCompleted events whose
    `status` field is one of {"timed_out", "completed", anything-else},
    mirroring `core.application.execution_service.ExecutionService` internal
    signalling. CLI translates those to the presentation-facing values
    "timeout" / "success" / "failed" without re-running any logic.
    """

    @pytest.fixture
    def forced_boss_id(self) -> UUID:
        """Pin the boss_id so events can be pre-seeded in the fake store."""
        return uuid4()

    @pytest.fixture
    def pinned_fake_service(
        self,
        fake_execution_service: FakeExecutionService,
        forced_boss_id: UUID,
    ) -> FakeExecutionService:
        """Patch the fake to produce a deterministic root agent id."""

        async def _create(
            task_description: str,
            domain_context: object | None = None,  # noqa: ARG001 - interface parity
        ) -> UUID:
            fake_execution_service.last_task_description = task_description
            fake_execution_service.created_boss_id = str(forced_boss_id)
            return forced_boss_id

        fake_execution_service.create_boss_agent = _create  # type: ignore[method-assign]
        return fake_execution_service

    @pytest.fixture
    def cli(
        self,
        pinned_fake_service: FakeExecutionService,
        fake_event_store: FakeEventStore,
        tmp_path: Path,
    ) -> CLI:
        config = CLIConfig(verbose=False, output_directory=str(tmp_path))
        return CLI(
            execution_service=pinned_fake_service,  # type: ignore[arg-type]
            event_store=fake_event_store,  # type: ignore[arg-type]
            config=config,
        )

    @staticmethod
    def _run_completed(boss_id: UUID, status: str) -> RunCompleted:
        return RunCompleted(
            aggregate_id=boss_id,
            sequence_number=0,
            status=status,
            duration_seconds=1.0,
            total_agents=1,
            completed_agents=1,
            failed_agents=0,
        )

    @pytest.mark.asyncio
    async def test_timed_out_status_maps_to_timeout(
        self,
        cli: CLI,
        fake_event_store: FakeEventStore,
        forced_boss_id: UUID,
    ) -> None:
        # Given: a RunCompleted event with status="timed_out" — what the
        # execution service emits via run_status_override on deadline hit.
        fake_event_store.set_events(
            forced_boss_id, [self._run_completed(forced_boss_id, "timed_out")]
        )

        # When
        result = await cli.run_task("task")

        # Then
        assert isinstance(result, RunResult)
        assert result.root_id == forced_boss_id
        assert result.status == "timeout"

    @pytest.mark.asyncio
    async def test_completed_status_maps_to_success(
        self,
        cli: CLI,
        fake_event_store: FakeEventStore,
        forced_boss_id: UUID,
    ) -> None:
        # Given: a normally completing run emits RunCompleted(status="completed").
        fake_event_store.set_events(
            forced_boss_id, [self._run_completed(forced_boss_id, "completed")]
        )

        # When
        result = await cli.run_task("task")

        # Then
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_exception_in_orchestration_maps_to_failed(
        self,
        cli: CLI,
        pinned_fake_service: FakeExecutionService,
    ) -> None:
        # Given: the orchestration loop raises — simulating an unexpected
        # runtime error. The event store is not consulted on the exception
        # path, so no RunCompleted events are needed.
        async def _boom(_root_id: object) -> None:
            raise RuntimeError("something exploded")

        pinned_fake_service.run_system_loop = _boom  # type: ignore[method-assign]

        # When
        result = await cli.run_task("task")

        # Then
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_missing_run_completed_event_maps_to_failed(
        self,
        cli: CLI,
        forced_boss_id: UUID,
    ) -> None:
        # Given: the orchestration loop returns cleanly but no RunCompleted
        # event has been persisted — treated as "failed" (not "success")
        # because we cannot prove success without the event.

        # When
        result = await cli.run_task("task")

        # Then
        assert result.root_id == forced_boss_id
        assert result.status == "failed"
