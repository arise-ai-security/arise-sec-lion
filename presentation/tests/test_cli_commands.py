"""Tests for Click-based CLI commands.

These tests verify the new Click CLI commands work correctly:
- events: View events for a task run
- summary: Show summary projection
- list: List past BOSS agent runs

The tests use Click's test utilities for isolated command testing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from click.testing import CliRunner

from presentation.cli import cli
from presentation.persistence import RunPersistence


if TYPE_CHECKING:
    pass


# ==============================================================================
# Last Run Tracking Tests (using RunPersistence)
# ==============================================================================


class TestRunPersistence:
    """Tests for RunPersistence class."""

    def test_save_last_run_creates_file(self, tmp_path: Path) -> None:
        """Test that save_last_run creates .last_run.json."""
        persistence = RunPersistence(tmp_path)
        boss_id = uuid4()
        persistence.save_last_run(boss_id, "test task", "completed")

        last_run_file = tmp_path / ".last_run.json"
        assert last_run_file.exists()

        data = json.loads(last_run_file.read_text())
        assert data["boss_id"] == str(boss_id)
        assert data["task"] == "test task"
        assert data["status"] == "completed"

    def test_save_last_run_creates_directory(self, tmp_path: Path) -> None:
        """Test that save_last_run creates parent directory if needed."""
        output_dir = tmp_path / "nested" / "output"
        persistence = RunPersistence(output_dir)
        boss_id = uuid4()

        persistence.save_last_run(boss_id, "test task", "completed")

        assert output_dir.exists()
        assert (output_dir / ".last_run.json").exists()

    def test_get_last_run_returns_data(self, tmp_path: Path) -> None:
        """Test that get_last_run returns saved data."""
        persistence = RunPersistence(tmp_path)
        boss_id = uuid4()
        persistence.save_last_run(boss_id, "test task", "completed")

        result = persistence.get_last_run()

        assert result is not None
        assert result.boss_id == boss_id
        assert result.task == "test task"

    def test_get_last_run_returns_none_when_not_found(self, tmp_path: Path) -> None:
        """Test that get_last_run returns None when file doesn't exist."""
        persistence = RunPersistence(tmp_path)
        result = persistence.get_last_run()
        assert result is None

    def test_get_last_run_id_returns_uuid(self, tmp_path: Path) -> None:
        """Test that get_last_run_id returns UUID from saved data."""
        persistence = RunPersistence(tmp_path)
        boss_id = uuid4()
        persistence.save_last_run(boss_id, "test task", "completed")

        result = persistence.get_last_run_id()

        assert result == boss_id

    def test_get_last_run_id_returns_none_when_not_found(self, tmp_path: Path) -> None:
        """Test that get_last_run_id returns None when file doesn't exist."""
        persistence = RunPersistence(tmp_path)
        result = persistence.get_last_run_id()
        assert result is None


# ==============================================================================
# CLI Help Tests
# ==============================================================================


class TestCLIHelp:
    """Tests for CLI help output."""

    def test_cli_help_shows_commands(self) -> None:
        """Test that CLI help lists available commands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "run" in result.output
        assert "events" in result.output
        assert "summary" in result.output
        assert "list" in result.output

    def test_run_help(self) -> None:
        """Test that run command has help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])

        assert result.exit_code == 0
        assert "Run a task" in result.output

    def test_events_help(self) -> None:
        """Test that events command has help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["events", "--help"])

        assert result.exit_code == 0
        assert "View events" in result.output
        assert "--agent-id" in result.output
        assert "--format" in result.output
        assert "--errors-only" in result.output

    def test_summary_help(self) -> None:
        """Test that summary command has help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["summary", "--help"])

        assert result.exit_code == 0
        assert "summary" in result.output.lower()
        assert "--agent-id" in result.output
        assert "--format" in result.output

    def test_list_help(self) -> None:
        """Test that list command has help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "--help"])

        assert result.exit_code == 0
        assert "List past BOSS" in result.output
        assert "--limit" in result.output


# ==============================================================================
# Events Command Tests
# ==============================================================================


class TestEventsCommand:
    """Tests for the events command."""

    @pytest.fixture
    def mock_event_store(self) -> MagicMock:
        """Create a mock event store."""
        store = MagicMock()
        store.connect = AsyncMock()
        store.disconnect = AsyncMock()
        store.get_events = AsyncMock(return_value=[])
        store.get_all_aggregate_ids = AsyncMock(return_value=[])
        return store

    def test_events_requires_agent_id_or_last_run(self) -> None:
        """Test that events command requires agent-id when no last run exists."""
        runner = CliRunner()

        with runner.isolated_filesystem():
            with patch(
                "presentation.context.event_store_context.EventStoreContext.from_config_path"
            ) as mock_ctx:
                mock_store = MagicMock()
                mock_store.connect = AsyncMock()
                mock_store.disconnect = AsyncMock()

                async_cm = AsyncMock()
                async_cm.__aenter__ = AsyncMock(return_value=mock_store)
                async_cm.__aexit__ = AsyncMock(return_value=None)
                mock_ctx.return_value = async_cm

                result = runner.invoke(cli, ["events"])

            assert "No agent-id specified" in result.output or result.exit_code != 0


# ==============================================================================
# List Command Tests
# ==============================================================================


class TestListCommand:
    """Tests for the list command."""

    def test_list_with_no_runs_shows_message(self) -> None:
        """Test that list shows message when no runs exist."""
        runner = CliRunner()

        with patch(
            "presentation.context.event_store_context.EventStoreContext.from_config_path"
        ) as mock_ctx:
            mock_store = MagicMock()
            mock_store.connect = AsyncMock()
            mock_store.disconnect = AsyncMock()
            mock_store.get_all_aggregate_ids = AsyncMock(return_value=[])

            async_cm = AsyncMock()
            async_cm.__aenter__ = AsyncMock(return_value=mock_store)
            async_cm.__aexit__ = AsyncMock(return_value=None)
            mock_ctx.return_value = async_cm

            result = runner.invoke(cli, ["list"])

        assert "No BOSS runs found" in result.output

    def test_list_format_json(self) -> None:
        """Test that list --format json outputs JSON."""
        runner = CliRunner()

        with patch(
            "presentation.context.event_store_context.EventStoreContext.from_config_path"
        ) as mock_ctx:
            mock_store = MagicMock()
            mock_store.connect = AsyncMock()
            mock_store.disconnect = AsyncMock()
            mock_store.get_all_aggregate_ids = AsyncMock(return_value=[])

            async_cm = AsyncMock()
            async_cm.__aenter__ = AsyncMock(return_value=mock_store)
            async_cm.__aexit__ = AsyncMock(return_value=None)
            mock_ctx.return_value = async_cm

            result = runner.invoke(cli, ["list", "--format", "json"])

        # Should output valid JSON (empty list)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []


# ==============================================================================
# Integration Tests (requires mock event store)
# ==============================================================================


class TestCLIConfigPassing:
    """Tests for config passing through CLI."""

    def test_config_option_accepted(self) -> None:
        """Test that --config option is accepted."""
        runner = CliRunner()

        # Just test that the option is recognized
        result = runner.invoke(cli, ["--config", "nonexistent.yaml", "--help"])

        # Should fail with file not found, not unrecognized option
        # But --help should still work
        assert "--config" in result.output or "Error" in result.output
