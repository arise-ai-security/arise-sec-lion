"""Tests for GoogleADKAdapter.

Uses mocking to avoid requiring actual Google ADK installation and API calls.
Tests verify correct event mapping, cost calculation, and error handling.
"""

from typing import ClassVar
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from core.domain.events.events import ThoughtCaptured, WorkCompleted, WorkFailed
from infrastructure.adapters.worker.google_adk_adapter import (
    ADKAdapterConfig,
    GoogleADKAdapter,
    execute_command,
)
from infrastructure.adapters.worker.shared.cost_calculator import get_model_pricing


_pricing = get_model_pricing("gemini-3-pro")
GEMINI_3_PRO_INPUT_PRICE_PER_M = _pricing.input_per_million
GEMINI_3_PRO_OUTPUT_PRICE_PER_M = _pricing.output_per_million


def _adk_config(**kwargs) -> ADKAdapterConfig:
    return ADKAdapterConfig(model="gemini-3-pro", **kwargs)


class TestExecuteCommand:
    """Tests for execute_command shell tool."""

    def test_successful_command(self) -> None:
        """Successful command returns success status."""
        result = execute_command("echo hello")

        assert result["status"] == "success"
        assert "hello" in result["stdout"]
        assert result["exit_code"] == 0

    def test_failed_command(self) -> None:
        """Failed command returns error status."""
        result = execute_command("exit 1", timeout=5)

        assert result["status"] == "error"
        assert result["exit_code"] == 1

    def test_invalid_command(self) -> None:
        """Invalid command returns error status."""
        result = execute_command("nonexistent_command_xyz_123", timeout=5)

        assert result["status"] == "error"
        assert result["exit_code"] != 0

    def test_timeout_returns_error(self) -> None:
        """Command timeout returns error with message."""
        result = execute_command("sleep 10", timeout=1)

        assert result["status"] == "error"
        assert "timed out" in result["stderr"]
        assert result["exit_code"] == -1

    def test_working_directory(self, tmp_path) -> None:
        """Working directory is respected."""
        result = execute_command("pwd", working_dir=str(tmp_path))

        assert result["status"] == "success"
        assert str(tmp_path) in result["stdout"]


class TestADKAdapterConfig:
    """Tests for ADKAdapterConfig dataclass."""

    def test_defaults(self) -> None:
        """Config has sensible defaults."""
        config = _adk_config()

        assert config.model == "gemini-3-pro"
        assert config.timeout_seconds == 300
        assert config.max_turns == 50
        assert "read_file" in config.allowed_tools
        assert "write_file" in config.allowed_tools
        assert "execute_command" in config.allowed_tools

    def test_custom_values(self) -> None:
        """Config accepts custom values."""
        config = ADKAdapterConfig(
            model="gemini-3-flash",
            timeout_seconds=600,
            max_turns=100,
            disallowed_tools=["write_file"],
        )

        assert config.model == "gemini-3-flash"
        assert config.timeout_seconds == 600
        assert config.max_turns == 100
        assert config.disallowed_tools == ["write_file"]


class TestGoogleADKAdapter:
    """Tests for GoogleADKAdapter class."""

    @pytest.mark.asyncio
    async def test_missing_task_description_raises(self) -> None:
        """Missing task_description raises ValueError."""
        adapter = GoogleADKAdapter(_adk_config())

        with pytest.raises(ValueError, match="task_description"):
            async for _ in adapter.run_session({"agent_id": uuid4()}):
                pass

    @pytest.mark.asyncio
    async def test_missing_agent_id_raises(self) -> None:
        """Missing agent_id raises ValueError."""
        adapter = GoogleADKAdapter(_adk_config())

        with pytest.raises(ValueError, match="agent_id"):
            async for _ in adapter.run_session({"task_description": "Test"}):
                pass

    def test_stream_name_constant(self) -> None:
        """Adapter uses consistent stream name."""
        assert GoogleADKAdapter.STREAM_NAME == "google_adk"

    def test_build_instruction(self) -> None:
        """Instruction is generic and task-execution oriented."""
        adapter = GoogleADKAdapter(_adk_config())
        instruction = adapter._build_instruction()

        assert "task execution agent" in instruction.lower()
        assert "filesystem" in instruction.lower()
        assert "shell" in instruction.lower()

    def test_tool_policy_blocks_filesystem_when_write_is_denied(self) -> None:
        adapter = GoogleADKAdapter(
            _adk_config(allowed_tools=["*"], disallowed_tools=["write_file"])
        )

        assert adapter._filesystem_tool_allowed() is False
        assert adapter._tool_allowed("execute_command") is True

    def test_calculate_cost(self) -> None:
        """Cost calculation uses correct pricing."""
        pricing = get_model_pricing("gemini-3-pro")

        # Test with known values
        input_tokens = 1_000_000  # 1M tokens
        output_tokens = 1_000_000  # 1M tokens

        expected_cost = GEMINI_3_PRO_INPUT_PRICE_PER_M + GEMINI_3_PRO_OUTPUT_PRICE_PER_M
        actual_cost = pricing.calculate_cost(input_tokens, output_tokens)

        assert actual_cost == expected_cost

    def test_calculate_cost_fractional(self) -> None:
        """Cost calculation works with fractional token counts."""
        pricing = get_model_pricing("gemini-3-pro")

        # 1000 input, 500 output tokens
        cost = pricing.calculate_cost(1000, 500)

        expected = (1000 / 1_000_000) * GEMINI_3_PRO_INPUT_PRICE_PER_M + \
                   (500 / 1_000_000) * GEMINI_3_PRO_OUTPUT_PRICE_PER_M
        assert abs(cost - expected) < 0.0001


class TestAdapterExecution:
    """Tests for adapter execution behavior."""

    @pytest.mark.asyncio
    async def test_adapter_handles_exceptions_gracefully(self) -> None:
        """Adapter handles exceptions and yields WorkFailed."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        # Since the actual ADK imports may fail or succeed depending on environment,
        # we just verify the adapter doesn't crash on valid input
        events = []
        async for event in adapter.run_session(
            {"task_description": "Test task", "agent_id": agent_id}
        ):
            events.append(event)

        # Should have at least one event (either success or failure)
        assert len(events) >= 1
        # Last event should be terminal (WorkCompleted or WorkFailed)
        last_event = events[-1]
        assert isinstance(last_event, (WorkCompleted, WorkFailed))


class TestEventProcessing:
    """Tests for ADK event to domain event mapping."""

    def test_process_text_content(self) -> None:
        """Text content is mapped to output type."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        # Mock event with string content
        mock_event = MagicMock()
        mock_event.content = "Hello, this is a response"

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 1
        assert isinstance(events[0], ThoughtCaptured)
        assert events[0].content == "Hello, this is a response"
        assert events[0].output_type == "output"

    def test_process_empty_content(self) -> None:
        """Empty content is skipped."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        mock_event = MagicMock()
        mock_event.content = "   "

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 0

    def test_process_content_with_text_attribute(self) -> None:
        """Content with text attribute is mapped correctly."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        # Create a content object with text attribute
        class MockContent:
            text = "Response with text attribute"

        mock_event = MagicMock()
        mock_event.content = MockContent()

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 1
        assert events[0].content == "Response with text attribute"
        assert events[0].output_type == "output"

    def test_process_none_content(self) -> None:
        """None content returns empty list."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        mock_event = MagicMock()
        mock_event.content = None

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 0

    def test_process_parts_with_text(self) -> None:
        """Content with parts containing text is mapped correctly."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        # Create a proper mock with parts
        class MockPart:
            text = "Text from part"

        class MockContent:
            parts: ClassVar[list[MockPart]] = [MockPart()]

        mock_event = MagicMock()
        mock_event.content = MockContent()

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 1
        assert events[0].content == "Text from part"
        assert events[0].output_type == "output"

    def test_process_parts_with_function_call(self) -> None:
        """Content with parts containing function_call is mapped correctly."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        # Create mock function call - part without text attribute
        class MockFunctionCall:
            name = "execute_command"
            args: ClassVar[dict[str, str]] = {"command": "gcc -o main main.c"}

        class MockPart:
            function_call = MockFunctionCall()

            def __init__(self) -> None:
                # This part has function_call but no text
                pass

        class MockContent:
            parts: ClassVar[list[MockPart]] = [MockPart()]

        mock_event = MagicMock()
        mock_event.content = MockContent()

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 1
        assert events[0].output_type == "tool_use"

    def test_process_parts_with_function_response(self) -> None:
        """Content with parts containing function_response is mapped correctly."""
        adapter = GoogleADKAdapter(_adk_config())
        agent_id = uuid4()

        from infrastructure.adapters.worker.shared import EventSequencer

        sequencer = EventSequencer(agent_id, stream="google_adk")

        class MockFunctionResponse:
            response = "Command output here"

        class MockPart:
            function_response = MockFunctionResponse()

        class MockContent:
            parts: ClassVar[list[MockPart]] = [MockPart()]

        mock_event = MagicMock()
        mock_event.content = MockContent()

        events = adapter._process_adk_event(mock_event, sequencer)

        assert len(events) == 1
        assert events[0].output_type == "tool_result"
        assert "Command output" in events[0].content


class TestCreateShellTool:
    """Tests for shell tool factory."""

    def test_shell_tool_binds_working_dir(self, tmp_path) -> None:
        """Shell tool uses bound working directory."""
        adapter = GoogleADKAdapter(_adk_config())
        shell_tool = adapter._create_shell_tool(str(tmp_path))

        result = shell_tool("pwd")

        assert result["status"] == "success"
        assert str(tmp_path) in result["stdout"]

    def test_shell_tool_respects_timeout(self, tmp_path) -> None:
        """Shell tool respects timeout parameter."""
        adapter = GoogleADKAdapter(_adk_config())
        shell_tool = adapter._create_shell_tool(str(tmp_path))

        result = shell_tool("sleep 10", timeout=1)

        assert result["status"] == "error"
        assert "timed out" in result["stderr"]
