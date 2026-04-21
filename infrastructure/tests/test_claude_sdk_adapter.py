"""Tests for ClaudeAgentSDKAdapter.

Uses mocking to avoid requiring actual Claude Agent SDK installation and API calls.
Tests verify correct event mapping and error handling.
"""

import time
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
from infrastructure.adapters.worker.shared.event_sequencer import EventSequencer


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

    def __init__(self, result: str, is_error: bool = False) -> None:
        self.result = result
        self.is_error = is_error


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
            permission_mode="default",
        )

        assert config.model == "claude-sonnet-4"
        assert config.timeout_seconds == 600
        assert config.allowed_tools == ["Read", "Bash"]
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

    @pytest.mark.asyncio
    async def test_run_session_prefers_task_context_model(self) -> None:
        """Runtime worker config overrides the adapter default model."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4"))
        agent_id = uuid4()
        seen: dict[str, str | None] = {}

        async def _fake_drain_queue(*_args, **_kwargs):
            if False:
                yield

        async def _fake_process_message(*_args, **_kwargs):
            if False:
                yield

        def _fake_build_options(
            _working_dir,
            _tool_queue,
            _container_session,
            runtime_model,
        ):
            seen["model"] = runtime_model
            return MagicMock()

        adapter._build_options = _fake_build_options  # type: ignore[method-assign]
        adapter._drain_queue = _fake_drain_queue  # type: ignore[method-assign]
        adapter._process_message = _fake_process_message  # type: ignore[method-assign]

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
                async for _event in adapter.run_session(
                    {
                        "task_description": "Use the escalated model",
                        "agent_id": agent_id,
                        "config": {"base": {"model": "claude-opus-4-20250514"}},
                    }
                ):
                    pass

        assert seen["model"] == "claude-opus-4-20250514"


def test_make_cost_event_parses_cache_and_thinking_tokens() -> None:
    """ResultMessage.usage is fully parsed into all 5 token dimensions on WorkerCostRecorded."""
    # Given: a Claude SDK ResultMessage with full usage dict including cache + reasoning tokens
    adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
    sequencer = EventSequencer(agent_id=uuid4(), stream="claude_sdk")
    message = MagicMock()
    message.total_cost_usd = 0.0123
    message.usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 200,
        "reasoning_tokens": 75,
    }

    # When: _make_cost_event is called
    event = adapter._make_cost_event(message, sequencer, runtime_model="claude-sonnet-4-6")

    # Then: all five token dimensions are captured on the resulting event
    assert event is not None
    assert isinstance(event, WorkerCostRecorded)
    assert event.prompt_tokens == 100
    assert event.completion_tokens == 50
    assert event.cache_read_tokens == 400
    assert event.cache_write_tokens == 200
    assert event.reasoning_tokens == 75
    assert event.tokens == 825  # total across all five
    assert event.cost_usd == 0.0123


def test_make_cost_event_preserves_explicit_zero_tokens() -> None:
    """Usage dict with all-zero values preserves zeros (not None) to distinguish 'no cache hits' from 'no data'."""
    # Given: a ResultMessage whose usage explicitly reports zeros on every dimension
    adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
    sequencer = EventSequencer(agent_id=uuid4(), stream="claude_sdk")
    message = MagicMock()
    message.total_cost_usd = 0.0
    message.usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    # When: _make_cost_event runs
    event = adapter._make_cost_event(message, sequencer, runtime_model="claude-sonnet-4-6")

    # Then: each dimensional field on the event is 0 (not None), total_tokens is 0, cost is 0.0
    assert event is not None
    assert isinstance(event, WorkerCostRecorded)
    assert event.prompt_tokens == 0
    assert event.completion_tokens == 0
    assert event.cache_read_tokens == 0
    assert event.cache_write_tokens == 0
    assert event.tokens == 0
    assert event.cost_usd == 0.0
    # And: reasoning_tokens key was absent from the usage dict, so it remains None
    assert event.reasoning_tokens is None


class TestPerToolCallDurationTracking:
    """Tests for PreToolUse/PostToolUse pairing that yields per-tool-call duration_ms."""

    def test_pretooluse_hook_records_start_time_for_correlation(self) -> None:
        """PreToolUse hook records a start timestamp keyed by tool_use_id."""
        # Given: a Claude SDK adapter with hooks initialized
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))

        # When: PreToolUse hook is invoked
        adapter._pre_tool_use_hook(tool_use_id="tu_abc123")

        # Then: adapter has recorded a start timestamp keyed by tool_use_id
        assert "tu_abc123" in adapter._pending_tool_starts
        assert isinstance(adapter._pending_tool_starts["tu_abc123"], float)

    def test_posttooluse_computes_duration_ms_from_pending_start(self) -> None:
        """PreToolUse + PostToolUse pair via tool_use_id to compute duration_ms."""
        # Given: a PreToolUse has recorded a start time
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
        adapter._pre_tool_use_hook(tool_use_id="tu_abc123")

        time.sleep(0.01)  # force a measurable duration

        # When: PostToolUse fires for the same tool_use_id
        duration_ms = adapter._get_and_pop_duration_ms("tu_abc123")

        # Then: duration is > 0 and the pending entry is cleared
        assert duration_ms is not None
        assert duration_ms >= 10  # slept 10ms; scheduler may add a small delta
        assert "tu_abc123" not in adapter._pending_tool_starts

    def test_posttooluse_returns_none_for_unknown_tool_use_id(self) -> None:
        """PostToolUse for a tool_use_id we didn't record returns None (no crash)."""
        # Given: no PreToolUse was recorded for this id
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))

        # When: asked for duration
        duration_ms = adapter._get_and_pop_duration_ms("nonexistent_id")

        # Then: returns None, doesn't raise
        assert duration_ms is None

    def test_build_options_registers_pre_and_post_tool_use_hooks(self, tmp_path) -> None:
        """Adapter registers PreToolUse hook alongside PostToolUse via HookMatcher.

        Verifies _build_options wires a PreToolUse entry with at least one callback.
        """
        # Given: an adapter and a tool queue
        import asyncio

        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
        tool_queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()

        # When: building options
        with patch(f"{SDK_ADAPTER_MODULE}.ClaudeAgentOptions") as mock_options_cls:
            adapter._build_options(
                working_dir=str(tmp_path),
                tool_queue=tool_queue,
                container_session=None,
                runtime_model=None,
            )

        # Then: the hooks kwarg passed to ClaudeAgentOptions has both PreToolUse
        # and PostToolUse keys
        _, kwargs = mock_options_cls.call_args
        hooks = kwargs["hooks"]
        assert "PreToolUse" in hooks
        assert "PostToolUse" in hooks

    def test_build_options_pins_system_prompt_to_claude_code_preset(self, tmp_path) -> None:
        """_build_options passes the explicit claude_code preset as system_prompt."""
        # Given: an adapter and a tool queue
        import asyncio

        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
        tool_queue: asyncio.Queue[
            tuple[str, str, str | None, dict[str, Any] | None]
        ] = asyncio.Queue()

        # When: building options
        with patch(f"{SDK_ADAPTER_MODULE}.ClaudeAgentOptions") as mock_options_cls:
            adapter._build_options(
                working_dir=str(tmp_path),
                tool_queue=tool_queue,
                container_session=None,
                runtime_model=None,
            )

        # Then: system_prompt kwarg is the explicit claude_code preset
        _, kwargs = mock_options_cls.call_args
        assert kwargs["system_prompt"] == {"type": "preset", "preset": "claude_code"}

    def test_tool_result_thought_carries_duration_and_call_id(self) -> None:
        """End-to-end: PreToolUse + sequencer.thought yields ThoughtCaptured with both fields."""
        # Given: an adapter that has recorded a pre-tool-use start for a known id
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
        adapter._pre_tool_use_hook(tool_use_id="tu_xyz")
        time.sleep(0.005)

        sequencer = EventSequencer(agent_id=uuid4(), stream="claude_sdk")

        # When: the PostToolUse path emits a thought annotated with the tool_use_id
        duration_ms = adapter._get_and_pop_duration_ms("tu_xyz")
        event = sequencer.thought(
            content="Tool result: ok",
            output_type="tool_result",
            call_id="tu_xyz",
            duration_ms=duration_ms,
        )

        # Then: the event carries both call_id and a positive duration_ms
        assert isinstance(event, ThoughtCaptured)
        assert event.call_id == "tu_xyz"
        assert event.duration_ms is not None
        assert event.duration_ms >= 5

    def test_reset_session_state_clears_pending_tool_starts(self) -> None:
        """Each new session clears any leftover start times from prior sessions."""
        # Given: an adapter with stale entries in _pending_tool_starts from a prior session
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))
        adapter._pending_tool_starts["stale_id_1"] = 12345.0
        adapter._pending_tool_starts["stale_id_2"] = 12346.0

        # When: a new session begins
        adapter._reset_session_state()

        # Then: the dict is empty
        assert adapter._pending_tool_starts == {}

    def test_get_and_pop_duration_ms_logs_debug_on_unknown_id(self, caplog) -> None:
        """Missing tool_use_id emits a DEBUG log so dataset holes are observable."""
        # Given: an adapter with empty pending dict
        import logging

        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(model="claude-sonnet-4-6"))

        # When: asked for duration of an id never recorded, with DEBUG capture enabled
        with caplog.at_level(
            logging.DEBUG,
            logger="infrastructure.adapters.worker.claude_sdk_adapter",
        ):
            result = adapter._get_and_pop_duration_ms("unknown_id_xyz")

        # Then: returns None, and a DEBUG message mentions the missing id
        assert result is None
        assert any("unknown_id_xyz" in rec.message for rec in caplog.records)
        assert any(rec.levelno == logging.DEBUG for rec in caplog.records)
