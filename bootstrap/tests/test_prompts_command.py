"""Integration tests for the 'prompts' CLI command.

Tests the full flow from CLI invocation to output.
"""

from datetime import UTC, datetime
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.domain.events.events import (
    AgentCreated,
    PromptSent,
    TaskAssigned,
)


def create_test_events(
    agent_id,
    role: str,
    task: str,
    prompt: str,
    parent_id=None,
    sibling_index: int = 0,
):
    """Create a set of test events for an agent."""
    return [
        AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role=role,
            parent_id=parent_id,
            sibling_index=sibling_index,
            config={},
        ),
        TaskAssigned(
            aggregate_id=agent_id,
            sequence_number=2,
            task_description=task,
        ),
        PromptSent(
            aggregate_id=agent_id,
            sequence_number=3,
            prompt=prompt,
            prompt_type="task_decomposition",
            target="llm",
        ),
    ]


class TestPromptsCommandIntegration:
    """Integration tests for prompts CLI command."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = MagicMock()
        settings.database.connection_string = "postgresql://test"
        settings.output.directory = "/tmp"
        return settings

    @pytest.fixture
    def mock_event_store(self):
        """Create mock event store."""
        store = MagicMock()
        store.connect = AsyncMock()
        store.disconnect = AsyncMock()
        store.get_hierarchy_events_grouped = AsyncMock()
        return store

    @pytest.mark.asyncio
    async def test_trace_prompts_outputs_tree_format(
        self, mock_settings, mock_event_store
    ) -> None:
        """Test that prompts command outputs tree format by default."""
        from bootstrap.bootstrap import _trace_prompts
        import argparse

        boss_id = uuid4()
        prompt = "<ROLE>You are a BOSS agent.</ROLE><TASK>Fix CVE</TASK>"

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            boss_id: create_test_events(boss_id, "boss", "Fix the CVE", prompt)
        }

        args = argparse.Namespace(
            config=None,
            agent_id=boss_id,
            depth=None,
            role=None,
            format="tree",
            section=None,
            output=None,
        )

        # Capture output
        output_lines = []

        with patch("bootstrap.bootstrap.Settings") as mock_settings_class, \
             patch("bootstrap.bootstrap.PostgresEventStore") as mock_store_class, \
             patch("presentation.persistence.RunPersistence"), \
             patch("builtins.print", side_effect=lambda x: output_lines.append(x)):

            mock_settings_class.load.return_value = mock_settings
            mock_store_class.return_value = mock_event_store

            await _trace_prompts(args)

        output = "\n".join(output_lines)
        assert "PROMPT TRACE:" in output
        assert "[BOSS]" in output
        assert "Fix the CVE" in output

    @pytest.mark.asyncio
    async def test_trace_prompts_outputs_json_format(
        self, mock_settings, mock_event_store
    ) -> None:
        """Test that prompts command outputs JSON when requested."""
        from bootstrap.bootstrap import _trace_prompts
        import argparse
        import json

        boss_id = uuid4()
        prompt = "<ROLE>You are a BOSS agent.</ROLE>"

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            boss_id: create_test_events(boss_id, "boss", "Test task", prompt)
        }

        args = argparse.Namespace(
            config=None,
            agent_id=boss_id,
            depth=None,
            role=None,
            format="json",
            section=None,
            output=None,
        )

        output_lines = []

        with patch("bootstrap.bootstrap.Settings") as mock_settings_class, \
             patch("bootstrap.bootstrap.PostgresEventStore") as mock_store_class, \
             patch("presentation.persistence.RunPersistence"), \
             patch("builtins.print", side_effect=lambda x: output_lines.append(x)):

            mock_settings_class.load.return_value = mock_settings
            mock_store_class.return_value = mock_event_store

            await _trace_prompts(args)

        output = "\n".join(output_lines)
        # Should be valid JSON
        data = json.loads(output)
        assert "root" in data
        assert data["root"]["role"] == "boss"

    @pytest.mark.asyncio
    async def test_trace_prompts_respects_depth_filter(
        self, mock_settings, mock_event_store
    ) -> None:
        """Test that depth filter works in CLI."""
        from bootstrap.bootstrap import _trace_prompts
        import argparse

        boss_id = uuid4()
        worker_id = uuid4()

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            boss_id: create_test_events(boss_id, "boss", "Boss task", "<ROLE>Boss</ROLE>"),
            worker_id: create_test_events(
                worker_id, "worker", "Worker task", "<ROLE>Worker</ROLE>",
                parent_id=boss_id,
            ),
        }

        args = argparse.Namespace(
            config=None,
            agent_id=boss_id,
            depth=0,  # Only show depth 0
            role=None,
            format="tree",
            section=None,
            output=None,
        )

        output_lines = []

        with patch("bootstrap.bootstrap.Settings") as mock_settings_class, \
             patch("bootstrap.bootstrap.PostgresEventStore") as mock_store_class, \
             patch("presentation.persistence.RunPersistence"), \
             patch("builtins.print", side_effect=lambda x: output_lines.append(x)):

            mock_settings_class.load.return_value = mock_settings
            mock_store_class.return_value = mock_event_store

            await _trace_prompts(args)

        output = "\n".join(output_lines)
        assert "[BOSS]" in output
        assert "Worker task" not in output  # Filtered out by depth=0

    @pytest.mark.asyncio
    async def test_trace_prompts_respects_role_filter(
        self, mock_settings, mock_event_store
    ) -> None:
        """Test that role filter works in CLI."""
        from bootstrap.bootstrap import _trace_prompts
        import argparse

        boss_id = uuid4()
        worker_id = uuid4()

        mock_event_store.get_hierarchy_events_grouped.return_value = {
            boss_id: create_test_events(boss_id, "boss", "Boss task", "<ROLE>Boss</ROLE>"),
            worker_id: create_test_events(
                worker_id, "worker", "Worker task", "<ROLE>Worker</ROLE>",
                parent_id=boss_id,
            ),
        }

        args = argparse.Namespace(
            config=None,
            agent_id=boss_id,
            depth=None,
            role="worker",  # Only show workers
            format="tree",
            section=None,
            output=None,
        )

        output_lines = []

        with patch("bootstrap.bootstrap.Settings") as mock_settings_class, \
             patch("bootstrap.bootstrap.PostgresEventStore") as mock_store_class, \
             patch("presentation.persistence.RunPersistence"), \
             patch("builtins.print", side_effect=lambda x: output_lines.append(x)):

            mock_settings_class.load.return_value = mock_settings
            mock_store_class.return_value = mock_event_store

            await _trace_prompts(args)

        output = "\n".join(output_lines)
        assert "[WORKER]" in output
        assert "[BOSS]" not in output  # Filtered out


class TestPromptsCommandArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_prompts_subcommand_exists(self) -> None:
        """Test that prompts subcommand is registered."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()
        args = parser.parse_args(["prompts"])

        assert args.command == "prompts"

    def test_prompts_format_options(self) -> None:
        """Test that format options are available."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()

        # Test each format option
        for fmt in ["tree", "json", "siblings"]:
            args = parser.parse_args(["prompts", "--format", fmt])
            assert args.format == fmt

    def test_prompts_depth_option(self) -> None:
        """Test that depth option is parsed."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()
        args = parser.parse_args(["prompts", "--depth", "2"])

        assert args.depth == 2

    def test_prompts_role_option(self) -> None:
        """Test that role option is parsed."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()

        for role in ["boss", "manager", "worker"]:
            args = parser.parse_args(["prompts", "--role", role])
            assert args.role == role

    def test_prompts_section_option(self) -> None:
        """Test that section filter option is parsed."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()
        args = parser.parse_args(["prompts", "--section", "parent-context"])

        assert args.section == "parent-context"

    def test_prompts_output_option(self) -> None:
        """Test that output file option is parsed."""
        from bootstrap.bootstrap import _create_parser
        from pathlib import Path

        parser = _create_parser()
        args = parser.parse_args(["prompts", "-o", "/tmp/trace.txt"])

        assert args.output == Path("/tmp/trace.txt")


class TestPromptsCommandErrorHandling:
    """Tests for error handling in prompts command."""

    @pytest.mark.asyncio
    async def test_no_agent_id_and_no_last_run(self) -> None:
        """Test error when no agent ID provided and no last run exists."""
        from bootstrap.bootstrap import _trace_prompts
        import argparse

        args = argparse.Namespace(
            config=None,
            agent_id=None,
            depth=None,
            role=None,
            format="tree",
            section=None,
            output=None,
        )

        output_lines = []
        mock_persistence = MagicMock()
        mock_persistence.get_last_run_id.return_value = None

        with patch("bootstrap.bootstrap.Settings") as mock_settings_class, \
             patch("presentation.persistence.RunPersistence", return_value=mock_persistence), \
             patch("builtins.print", side_effect=lambda x: output_lines.append(x)):

            mock_settings = MagicMock()
            mock_settings.output.directory = "/tmp"
            mock_settings_class.load.return_value = mock_settings

            await _trace_prompts(args)

        output = "\n".join(output_lines)
        assert "Error" in output or "No agent-id" in output
