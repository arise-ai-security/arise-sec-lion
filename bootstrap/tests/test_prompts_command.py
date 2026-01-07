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
            format="tree",
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
            format="json",
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


class TestPromptsCommandArgumentParsing:
    """Tests for CLI argument parsing."""

    def test_prompts_subcommand_exists(self) -> None:
        """Test that prompts subcommand is registered."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()
        args = parser.parse_args(["prompts"])

        assert args.command == "prompts"

    def test_prompts_format_options(self) -> None:
        """Test that format options are available (simplified to tree/json only)."""
        from bootstrap.bootstrap import _create_parser

        parser = _create_parser()

        # Test each format option (simplified CLI: only tree and json)
        for fmt in ["tree", "json"]:
            args = parser.parse_args(["prompts", "--format", fmt])
            assert args.format == fmt


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
            format="tree",
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
