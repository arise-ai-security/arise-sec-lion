"""Integration tests for CLI presentation layer.

These tests verify CLI behavior with mocked application services.
The CLI should correctly:
- Parse command line arguments
- Display appropriate output
- Delegate to execution service
- Handle errors gracefully
"""

import sys
from io import StringIO
from unittest.mock import patch

import pytest

from presentation.cli import CLI, CLIConfig

from .conftest import FakeExecutionService


class TestCLIArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_parse_single_word_task(self, cli_with_fake_service: CLI) -> None:
        """Test parsing a single word task."""
        with patch.object(sys, "argv", ["main.py", "test"]):
            result = cli_with_fake_service._parse_arguments()
        assert result == "test"

    def test_parse_multi_word_task(self, cli_with_fake_service: CLI) -> None:
        """Test parsing a multi-word task (words joined with spaces)."""
        with patch.object(sys, "argv", ["main.py", "Build", "a", "REST", "API"]):
            result = cli_with_fake_service._parse_arguments()
        assert result == "Build a REST API"

    def test_parse_quoted_task(self, cli_with_fake_service: CLI) -> None:
        """Test parsing a quoted task description."""
        with patch.object(sys, "argv", ["main.py", "Build a REST API with auth"]):
            result = cli_with_fake_service._parse_arguments()
        assert result == "Build a REST API with auth"

    def test_parse_no_arguments_returns_none(self, cli_with_fake_service: CLI) -> None:
        """Test that missing task returns None."""
        with patch.object(sys, "argv", ["main.py"]):
            result = cli_with_fake_service._parse_arguments()
        assert result is None


class TestCLIBanner:
    """Tests for CLI banner display."""

    def test_print_banner_outputs_header(self, cli_with_fake_service: CLI) -> None:
        """Test that banner prints the application header."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            cli_with_fake_service._print_banner()

        output = captured.getvalue()
        assert "Recursive Multi-Agent System" in output
        assert "Event Sourcing" in output

    def test_print_usage_shows_examples(self, cli_with_fake_service: CLI) -> None:
        """Test that usage shows example commands."""
        captured = StringIO()
        with patch("sys.stdout", captured):
            cli_with_fake_service._print_usage()

        output = captured.getvalue()
        assert "Usage:" in output
        assert "python main.py" in output
        assert "Examples:" in output


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
        with patch("sys.stdout", captured):
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
        with patch("sys.stdout", captured):
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
        with patch("sys.stdout", captured):
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
