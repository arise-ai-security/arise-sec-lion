"""Tests for ClaudeAgentSDKAdapter.

Uses mocking to avoid requiring actual Claude Agent SDK installation and API calls.
Tests verify correct event mapping and error handling.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.domain.events.events import (
    ThoughtCaptured,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from infrastructure.adapters.worker.claude_sdk_adapter import (
    ClaudeAgentSDKAdapter,
    SDKAdapterConfig,
)
from infrastructure.adapters.worker.shared import EventSequencer


# Module path for patching (use actual implementation module)
SDK_ADAPTER_MODULE = "infrastructure.adapters.worker.claude_sdk_adapter"


class MockTextBlock:
    """Mock TextBlock from claude-agent-sdk."""

    def __init__(self, text: str) -> None:
        self.text = text


class MockThinkingBlock:
    """Mock ThinkingBlock from claude-agent-sdk."""

    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class MockToolResultBlock:
    """Mock ToolResultBlock from claude-agent-sdk."""

    def __init__(self, content: str) -> None:
        self.content = content


class MockAssistantMessage:
    """Mock AssistantMessage from claude-agent-sdk."""

    def __init__(self, content: list) -> None:
        self.content = content


class MockResultMessage:
    """Mock ResultMessage from claude-agent-sdk."""

    def __init__(
        self,
        result: str,
        is_error: bool = False,
        usage: dict[str, int] | None = None,
        total_cost_usd: float | None = None,
    ) -> None:
        self.result = result
        self.is_error = is_error
        self.usage = usage or {}
        self.total_cost_usd = total_cost_usd


async def async_iter(items):
    """Helper to create async iterator from list."""
    for item in items:
        yield item


class TestProcessBlock:
    """Tests for _process_block pure function."""

    def test_text_block_returns_output(self) -> None:
        """TextBlock maps to output type."""
        block = MockTextBlock("Hello world")

        with patch(
            f"{SDK_ADAPTER_MODULE}.TextBlock", MockTextBlock
        ):
            result = ClaudeAgentSDKAdapter._process_block(block)

        assert result == ("Hello world", "output")

    def test_text_block_empty_returns_none(self) -> None:
        """Empty TextBlock is skipped."""
        block = MockTextBlock("   ")

        with patch(
            f"{SDK_ADAPTER_MODULE}.TextBlock", MockTextBlock
        ):
            result = ClaudeAgentSDKAdapter._process_block(block)

        assert result is None

    def test_tool_result_block_returns_tool_result(self) -> None:
        """ToolResultBlock maps to tool_result type."""
        block = MockToolResultBlock("command output here")

        with patch(
            f"{SDK_ADAPTER_MODULE}.ToolResultBlock",
            MockToolResultBlock,
        ):
            result = ClaudeAgentSDKAdapter._process_block(block)

        assert result == ("Tool result: command output here", "tool_result")

    def test_tool_result_block_empty_content(self) -> None:
        """ToolResultBlock with no content shows placeholder."""
        block = MockToolResultBlock("")

        with patch(
            f"{SDK_ADAPTER_MODULE}.ToolResultBlock",
            MockToolResultBlock,
        ):
            result = ClaudeAgentSDKAdapter._process_block(block)

        assert result == ("Tool result: (no output)", "tool_result")

    def test_tool_result_truncates_long_content(self) -> None:
        """ToolResultBlock truncates content over 500 chars."""
        long_content = "x" * 600
        block = MockToolResultBlock(long_content)

        with patch(
            f"{SDK_ADAPTER_MODULE}.ToolResultBlock",
            MockToolResultBlock,
        ):
            result = ClaudeAgentSDKAdapter._process_block(block)

        assert result is not None
        content, _ = result
        assert len(content) < 520  # "Tool result: " + 500 chars

    def test_unknown_block_returns_none(self) -> None:
        """Unknown block types are skipped."""

        class UnknownBlock:
            pass

        result = ClaudeAgentSDKAdapter._process_block(UnknownBlock())
        assert result is None


class TestSDKAdapterConfig:
    """Tests for SDKAdapterConfig dataclass."""

    def test_defaults(self) -> None:
        """Config has sensible defaults."""
        config = SDKAdapterConfig()

        assert config.model is None
        assert config.timeout_seconds == 300
        assert "Read" in config.allowed_tools
        assert "Write" in config.allowed_tools
        assert "Edit" in config.allowed_tools
        assert "Bash" in config.allowed_tools
        assert config.permission_mode == "bypassPermissions"

    def test_custom_values(self) -> None:
        """Config accepts custom values."""
        config = SDKAdapterConfig(
            model="claude-sonnet-4",
            timeout_seconds=600,
            allowed_tools=["Read", "Bash"],
            disallowed_tools=["Write"],
            permission_mode="default",
        )

        assert config.model == "claude-sonnet-4"
        assert config.timeout_seconds == 600
        assert config.allowed_tools == ["Read", "Bash"]
        assert config.disallowed_tools == ["Write"]
        assert config.permission_mode == "default"


class TestClaudeAgentSDKAdapter:
    """Tests for ClaudeAgentSDKAdapter class."""

    @pytest.mark.asyncio
    async def test_missing_task_description_raises(self) -> None:
        """Missing task_description raises ValueError."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())

        with pytest.raises(ValueError, match="task_description"):
            async for _ in adapter.run_session({"agent_id": uuid4()}):
                pass

    @pytest.mark.asyncio
    async def test_missing_agent_id_raises(self) -> None:
        """Missing agent_id raises ValueError."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())

        with pytest.raises(ValueError, match="agent_id"):
            async for _ in adapter.run_session({"task_description": "Test"}):
                pass

    def test_stream_name_constant(self) -> None:
        """Adapter uses consistent stream name."""
        assert ClaudeAgentSDKAdapter.STREAM_NAME == "claude_sdk"

    def test_effective_allowed_tools_applies_disallowed_policy(self) -> None:
        adapter = ClaudeAgentSDKAdapter(
            SDKAdapterConfig(allowed_tools=["*"], disallowed_tools=["Write", "MultiEdit"])
        )

        allowed = adapter._effective_allowed_tools()

        assert "Read" in allowed
        assert "Bash" in allowed
        assert "Write" not in allowed
        assert "MultiEdit" not in allowed

    def test_format_error_cli_not_found(self) -> None:
        """CLI not found errors get helpful message."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        error = Exception("CLI not found in PATH")
        result = adapter._format_error(error)

        assert "reinstalling" in result
        assert "pip install" in result

    def test_format_error_generic(self) -> None:
        """Generic errors include repr."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        error = ValueError("Something went wrong")
        result = adapter._format_error(error)

        assert "Claude SDK adapter error" in result
        assert "ValueError" in result

    def test_make_cost_event_records_token_breakdown(self) -> None:
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
        agent_id = uuid4()
        message = MockResultMessage(
            result="done",
            usage={
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
                "thinking_tokens": 7,
            },
            total_cost_usd=0.0123,
        )

        event = adapter._make_cost_event(
            message,
            sequencer=EventSequencer(agent_id, stream="claude_sdk"),
        )

        assert isinstance(event, WorkerCostRecorded)
        assert event.model == "claude-sonnet-4-6"
        assert event.tokens == 125
        assert event.prompt_tokens == 100
        assert event.completion_tokens == 25
        assert event.cache_read_tokens == 10
        assert event.cache_write_tokens == 5
        assert event.reasoning_tokens == 7
        assert event.cost_usd == 0.0123


class TestAdapterIntegration:
    """Integration tests with mocked SDK."""

    @pytest.mark.asyncio
    async def test_successful_execution(self) -> None:
        """Successful execution yields ThoughtCaptured and WorkCompleted."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        agent_id = uuid4()

        text_block = MockTextBlock("Hello, I completed the task")
        assistant_msg = MockAssistantMessage([text_block])
        result_msg = MockResultMessage(result="Task completed successfully")

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": MagicMock(
                    AssistantMessage=MockAssistantMessage,
                    ResultMessage=MockResultMessage,
                    TextBlock=MockTextBlock,
                    ToolResultBlock=MockToolResultBlock,
                    ClaudeAgentOptions=MagicMock,
                    ClaudeSDKClient=MagicMock,
                    HookMatcher=MagicMock,
                ),
            },
        ):
            from infrastructure.adapters.worker import claude_sdk_adapter

            mock_client = AsyncMock()
            mock_client.query = AsyncMock()
            mock_client.receive_response = MagicMock(
                return_value=async_iter([assistant_msg, result_msg])
            )

            mock_client_class = MagicMock()
            mock_client_class.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_class.__aexit__ = AsyncMock(return_value=None)

            with patch.object(
                claude_sdk_adapter, "ClaudeSDKClient", return_value=mock_client_class
            ), patch.object(
                claude_sdk_adapter, "AssistantMessage", MockAssistantMessage
            ), patch.object(
                claude_sdk_adapter, "ResultMessage", MockResultMessage
            ), patch.object(
                claude_sdk_adapter, "TextBlock", MockTextBlock
            ):
                events = []
                async for event in adapter.run_session(
                    {
                        "task_description": "Write a hello script",
                        "agent_id": agent_id,
                    }
                ):
                    events.append(event)

                thought_events = [e for e in events if isinstance(e, ThoughtCaptured)]
                assert len(thought_events) >= 1
                assert thought_events[0].content == "Hello, I completed the task"
                assert thought_events[0].stream == "claude_sdk"
                assert thought_events[0].output_type == "output"

                assert isinstance(events[-1], WorkCompleted)
                assert events[-1].result == "Task completed successfully"

    @pytest.mark.asyncio
    async def test_error_result(self) -> None:
        """Error result yields WorkFailed."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        agent_id = uuid4()

        result_msg = MockResultMessage(result="Task failed: file not found", is_error=True)

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": MagicMock(
                    AssistantMessage=MockAssistantMessage,
                    ResultMessage=MockResultMessage,
                    TextBlock=MockTextBlock,
                    ToolResultBlock=MockToolResultBlock,
                    ClaudeAgentOptions=MagicMock,
                    ClaudeSDKClient=MagicMock,
                    HookMatcher=MagicMock,
                ),
            },
        ):
            from infrastructure.adapters.worker import claude_sdk_adapter

            mock_client = AsyncMock()
            mock_client.query = AsyncMock()
            mock_client.receive_response = MagicMock(
                return_value=async_iter([result_msg])
            )

            mock_client_class = MagicMock()
            mock_client_class.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_class.__aexit__ = AsyncMock(return_value=None)

            with patch.object(
                claude_sdk_adapter, "ClaudeSDKClient", return_value=mock_client_class
            ), patch.object(
                claude_sdk_adapter, "ResultMessage", MockResultMessage
            ):
                events = []
                async for event in adapter.run_session(
                    {
                        "task_description": "Test task",
                        "agent_id": agent_id,
                    }
                ):
                    events.append(event)

                assert len(events) == 1
                assert isinstance(events[0], WorkFailed)
                assert "file not found" in events[0].reason
