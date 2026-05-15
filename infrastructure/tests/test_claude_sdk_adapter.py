"""Tests for ClaudeAgentSDKAdapter.

Uses mocking to avoid requiring actual Claude Agent SDK installation and API calls.
Tests verify correct event mapping and error handling.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.domain.events.events import (
    ThoughtCaptured,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from infrastructure.adapters.worker import claude_sdk_adapter
from infrastructure.adapters.worker.claude_sdk_adapter import (
    CLAUDE_CODE_CLI_PATH_ENV,
    DEFAULT_MAX_THINKING_TOKENS,
    ClaudeAgentSDKAdapter,
    SDKAdapterConfig,
)
from infrastructure.adapters.worker.shared import ContainerSessionContext, EventSequencer


# Module path for patching (use actual implementation module)
SDK_ADAPTER_MODULE = "infrastructure.adapters.worker.claude_sdk_adapter"


def _container_session(tmp_path: Path) -> ContainerSessionContext:
    return ContainerSessionContext(
        container_id="abc123def456",
        container_name="secbench-worker-demo",
        image="secb-tools:demo.cve-2024-0001-patch",
        workspace_root=tmp_path,
        host_source_dir=tmp_path / "src",
        host_testcase_dir=tmp_path / "testcase",
        host_work_dir=tmp_path / "src" / "demo",
        container_source_dir="/src",
        container_testcase_dir="/testcase",
        container_working_directory="/src/demo",
        helper_script=tmp_path / "secb-exec",
    )


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

        with patch(f"{SDK_ADAPTER_MODULE}.TextBlock", MockTextBlock):
            result = ClaudeAgentSDKAdapter._process_block(block)

        assert result == ("Hello world", "output")

    def test_text_block_empty_returns_none(self) -> None:
        """Empty TextBlock is skipped."""
        block = MockTextBlock("   ")

        with patch(f"{SDK_ADAPTER_MODULE}.TextBlock", MockTextBlock):
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

    def test_text_block_rewrites_host_paths_for_events(self, tmp_path: Path) -> None:
        """TextBlock event content hides run-scoped host mirror paths."""
        # Given: a Claude text block containing a host mirror path.
        session = _container_session(tmp_path)
        host_path = tmp_path / "testcase" / "base_commit_hash"
        block = MockTextBlock(f"Read {host_path}")

        # When: the adapter converts the block into an event payload.
        with patch(f"{SDK_ADAPTER_MODULE}.TextBlock", MockTextBlock):
            result = ClaudeAgentSDKAdapter._process_block(block, session)

        # Then: the emitted content uses the canonical container path.
        assert result == ("Read /testcase/base_commit_hash", "output")

    def test_thinking_block_rewrites_host_paths_for_events(
        self,
        tmp_path: Path,
    ) -> None:
        """ThinkingBlock event content hides run-scoped host mirror paths."""
        # Given: a Claude thinking block containing a host mirror path.
        session = _container_session(tmp_path)
        host_path = tmp_path / "src" / "demo" / "main.c"
        block = MockThinkingBlock(f"Need to inspect {host_path}")

        # When: the adapter converts the block into an event payload.
        with patch(f"{SDK_ADAPTER_MODULE}.ThinkingBlock", MockThinkingBlock):
            result = ClaudeAgentSDKAdapter._process_block(block, session)

        # Then: the emitted content uses the canonical container path.
        assert result == ("Need to inspect /src/demo/main.c", "thinking")

    def test_tool_result_block_rewrites_host_paths_for_events(
        self,
        tmp_path: Path,
    ) -> None:
        """ToolResultBlock event content hides run-scoped host mirror paths."""
        # Given: a tool result containing a host mirror path.
        session = _container_session(tmp_path)
        host_path = tmp_path / "testcase" / "base_commit_hash"
        block = MockToolResultBlock(f"{host_path}: f659")

        # When: the adapter converts the block into an event payload.
        with patch(f"{SDK_ADAPTER_MODULE}.ToolResultBlock", MockToolResultBlock):
            result = ClaudeAgentSDKAdapter._process_block(block, session)

        # Then: the emitted content uses the canonical container path.
        assert result == ("Tool result: /testcase/base_commit_hash: f659", "tool_result")

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
        assert config.max_thinking_tokens == DEFAULT_MAX_THINKING_TOKENS
        assert config.thinking_display == "summarized"
        assert config.cli_path is None

    def test_custom_values(self) -> None:
        """Config accepts custom values."""
        config = SDKAdapterConfig(
            model="claude-sonnet-4",
            timeout_seconds=600,
            allowed_tools=["Read", "Bash"],
            disallowed_tools=["Write"],
            permission_mode="default",
            max_thinking_tokens=None,
            thinking_display=None,
            cli_path="/usr/local/bin/claude",
        )

        assert config.model == "claude-sonnet-4"
        assert config.timeout_seconds == 600
        assert config.allowed_tools == ["Read", "Bash"]
        assert config.disallowed_tools == ["Write"]
        assert config.permission_mode == "default"
        assert config.max_thinking_tokens is None
        assert config.thinking_display is None
        assert config.cli_path == "/usr/local/bin/claude"


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

    def test_build_options_passes_tool_policy_verbatim_to_sdk(self) -> None:
        """Configured allowlist and denylist reach ClaudeAgentOptions unchanged."""
        # Given: an adapter with explicit built-in and custom tool policy.
        adapter = ClaudeAgentSDKAdapter(
            SDKAdapterConfig(
                allowed_tools=["Read", "Bash", "mcp__custom_tools__run"],
                disallowed_tools=["WebFetch", "Task"],
            )
        )
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from infrastructure.adapters.worker import claude_sdk_adapter

        # When: building SDK options.
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=MagicMock(),
                container_session=None,
            )

        # Then: policy is passed through for Claude SDK validation/enforcement.
        assert captured["allowed_tools"] == ["Read", "Bash", "mcp__custom_tools__run"]
        assert captured["disallowed_tools"] == ["WebFetch", "Task"]

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

        # Audit N-3: `tokens` is the INCLUSIVE total across all five buckets,
        # not just prompt+completion (which historically undercounted cache
        # and reasoning and made cross-cell token comparisons asymmetric).
        assert isinstance(event, WorkerCostRecorded)
        assert event.model == "claude-sonnet-4-6"
        assert event.tokens == 100 + 25 + 10 + 5 + 7
        assert event.prompt_tokens == 100
        assert event.completion_tokens == 25
        assert event.cache_read_tokens == 10
        assert event.cache_write_tokens == 5
        assert event.reasoning_tokens == 7
        assert event.cost_usd == 0.0123

    def test_make_result_event_rewrites_host_paths_for_events(
        self,
        tmp_path: Path,
    ) -> None:
        """Terminal WorkCompleted content hides run-scoped host mirror paths."""
        # Given: a result message containing a host mirror path.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        agent_id = uuid4()
        session = _container_session(tmp_path)
        host_path = tmp_path / "src" / "demo" / "main.c"
        message = MockResultMessage(result=f"Updated {host_path}")

        # When: the adapter emits the terminal event.
        event = adapter._make_result_event(
            message,
            sequencer=EventSequencer(agent_id, stream="claude_sdk"),
            container_session=session,
        )

        # Then: the event uses the canonical container path.
        assert isinstance(event, WorkCompleted)
        assert event.result == "Updated /src/demo/main.c"


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

            with (
                patch.object(claude_sdk_adapter, "ClaudeSDKClient", return_value=mock_client_class),
                patch.object(claude_sdk_adapter, "AssistantMessage", MockAssistantMessage),
                patch.object(claude_sdk_adapter, "ResultMessage", MockResultMessage),
                patch.object(claude_sdk_adapter, "TextBlock", MockTextBlock),
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
            mock_client.receive_response = MagicMock(return_value=async_iter([result_msg]))

            mock_client_class = MagicMock()
            mock_client_class.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_class.__aexit__ = AsyncMock(return_value=None)

            with (
                patch.object(claude_sdk_adapter, "ClaudeSDKClient", return_value=mock_client_class),
                patch.object(claude_sdk_adapter, "ResultMessage", MockResultMessage),
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
    async def test_query_timeout_yields_work_failed(self) -> None:
        """A hanging SDK query is bounded by the adapter timeout."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(timeout_seconds=0.01))

        async def _hang(_message: str) -> None:
            await asyncio.sleep(10)

        from infrastructure.adapters.worker import claude_sdk_adapter

        mock_client = AsyncMock()
        mock_client.query = AsyncMock(side_effect=_hang)
        mock_client.receive_response = MagicMock(return_value=async_iter([]))

        mock_client_class = MagicMock()
        mock_client_class.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.__aexit__ = AsyncMock(return_value=None)

        with patch.object(claude_sdk_adapter, "ClaudeSDKClient", return_value=mock_client_class):
            events = []
            async for event in adapter.run_session(
                {
                    "task_description": "Test task",
                    "agent_id": uuid4(),
                }
            ):
                events.append(event)

        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason

    @pytest.mark.asyncio
    async def test_receive_response_timeout_yields_work_failed(self) -> None:
        """A hanging SDK response stream is bounded by the adapter timeout."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig(timeout_seconds=0.01))

        async def _hang_stream():
            await asyncio.sleep(10)
            if False:
                yield None

        from infrastructure.adapters.worker import claude_sdk_adapter

        mock_client = AsyncMock()
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_hang_stream())

        mock_client_class = MagicMock()
        mock_client_class.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_class.__aexit__ = AsyncMock(return_value=None)

        with patch.object(claude_sdk_adapter, "ClaudeSDKClient", return_value=mock_client_class):
            events = []
            async for event in adapter.run_session(
                {
                    "task_description": "Test task",
                    "agent_id": uuid4(),
                }
            ):
                events.append(event)

        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason


class TestMCPServersWiring:
    """Verify task_context['mcp_servers'] reaches ClaudeAgentOptions."""

    def test_extract_mcp_servers_returns_none_when_absent(self) -> None:
        # Given/When/Then
        assert ClaudeAgentSDKAdapter._extract_mcp_servers({}) is None
        assert ClaudeAgentSDKAdapter._extract_mcp_servers({"mcp_servers": {}}) is None

    def test_extract_mcp_servers_adds_stdio_type_per_entry(self) -> None:
        # Given: a raw stdio spec keyed by server name.
        raw = {
            "custom_tools": {
                "command": "python",
                "args": ["-m", "example.tools_server"],
                "env": {"ARISE_CONTAINER_ID": "abc"},
            }
        }

        # When
        result = ClaudeAgentSDKAdapter._extract_mcp_servers({"mcp_servers": raw})

        # Then: the SDK-shaped mapping carries type=stdio.
        assert result is not None
        assert result["custom_tools"]["type"] == "stdio"
        assert result["custom_tools"]["command"] == "python"
        assert result["custom_tools"]["env"]["ARISE_CONTAINER_ID"] == "abc"

    def test_build_options_passes_mcp_servers_to_claude_agent_options(self) -> None:
        """``mcp_servers`` keyword reaches ``ClaudeAgentOptions`` constructor."""
        # Given: an adapter and a captured ClaudeAgentOptions stub.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from infrastructure.adapters.worker import claude_sdk_adapter

        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=MagicMock(),
                container_session=None,
                mcp_servers={
                    "custom_tools": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["-m", "example.tools_server"],
                        "env": {},
                    }
                },
            )

        # Then: ClaudeAgentOptions received a mcp_servers kwarg with the spec.
        assert "mcp_servers" in captured
        assert "custom_tools" in captured["mcp_servers"]  # type: ignore[index]
        assert captured["mcp_servers"]["custom_tools"]["command"] == "python"  # type: ignore[index]

    def test_build_options_omits_mcp_servers_when_unset(self) -> None:
        """No ``mcp_servers`` kwarg when not provided."""
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from infrastructure.adapters.worker import claude_sdk_adapter

        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=MagicMock(),
                container_session=None,
            )

        assert "mcp_servers" not in captured

    def test_build_options_enables_summarized_thinking_by_default(self) -> None:
        """Default B-cell SDK options request visible Claude thinking blocks."""
        # Given: an adapter and a captured ClaudeAgentOptions stub.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from infrastructure.adapters.worker import claude_sdk_adapter

        # When: building SDK options.
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=MagicMock(),
                container_session=None,
            )

        # Then: ClaudeAgentOptions receives fixed-budget thinking and summarized output.
        assert captured["max_thinking_tokens"] == DEFAULT_MAX_THINKING_TOKENS
        assert captured["extra_args"] == {"thinking-display": "summarized"}

    def test_build_options_uses_claude_cli_path_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Env override selects the Claude Code executable used by the SDK."""
        # Given: an environment override for the SDK transport's CLI path.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        monkeypatch.setenv(CLAUDE_CODE_CLI_PATH_ENV, " /usr/local/bin/claude ")
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from infrastructure.adapters.worker import claude_sdk_adapter

        # When: building SDK options.
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=MagicMock(),
                container_session=None,
            )

        # Then: ClaudeAgentOptions bypasses the SDK bundled CLI.
        assert captured["cli_path"] == "/usr/local/bin/claude"

    def test_configured_cli_path_overrides_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit config path wins over the process environment."""
        # Given: both an env override and an explicit adapter config path.
        adapter = ClaudeAgentSDKAdapter(
            SDKAdapterConfig(cli_path="/opt/claude/bin/claude")
        )
        monkeypatch.setenv(CLAUDE_CODE_CLI_PATH_ENV, "/usr/local/bin/claude")
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from infrastructure.adapters.worker import claude_sdk_adapter

        # When: building SDK options.
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=MagicMock(),
                container_session=None,
            )

        # Then: explicit config remains the strongest override.
        assert captured["cli_path"] == "/opt/claude/bin/claude"


class MockHookInput:
    """Stub for claude_agent_sdk HookInput with attributes the hook reads."""

    def __init__(self, tool_name: str, tool_input: dict) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input


def _extract_capture_tool_use_hook(captured_kwargs: dict[str, object]):
    """Pull the PostToolUse callback out of the ClaudeAgentOptions kwargs."""
    hooks = captured_kwargs["hooks"]
    assert isinstance(hooks, dict)
    matchers = hooks["PostToolUse"]
    assert isinstance(matchers, list)
    assert matchers
    # HookMatcher is a dataclass with a public `hooks` list of callables.
    return matchers[0].hooks[0]


class TestToolUseInputPayload:
    """BUG-EVENT1 regression: tool_use events must include the input payload.

    Sibling adapters (`claude_code_worker.py:565-567`, `openhands_adapter.py:692`)
    append ``\\nInput: {<json>}`` to tool_use content so the cross-cell
    cheating-attempt detector in `experiments/shared/scripts/run_metrics.py`
    can recover the Bash command string. Pre-fix, this adapter dropped the
    payload and made B-cell Bash commands invisible to the detector.
    """

    @pytest.mark.asyncio
    async def test_capture_tool_use_appends_input_json_payload(self) -> None:
        """Hook content carries ``\\nInput: {...}`` with the Bash command string."""
        # Given: an adapter and a fake ClaudeAgentOptions that captures kwargs.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        import asyncio as _asyncio

        from infrastructure.adapters.worker import claude_sdk_adapter

        tool_queue: _asyncio.Queue[tuple[str, str, str | None]] = _asyncio.Queue()
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=tool_queue,
                container_session=None,
            )

        capture_tool_use = _extract_capture_tool_use_hook(captured)

        # When: the PostToolUse hook fires for a Bash command.
        hook_input = MockHookInput(
            tool_name="Bash",
            tool_input={"command": "git log --oneline -3"},
        )
        await capture_tool_use(hook_input, None, MagicMock())

        # Then: the queued content carries the canonical `\nInput: {<json>}` suffix
        # AND the literal command string.
        content, output_type, tool_name = tool_queue.get_nowait()
        assert output_type == "tool_use"
        assert tool_name == "Bash"
        assert "\nInput: " in content, (
            "tool_use content must include the `\\nInput: ` JSON suffix so the "
            "cheating-attempt detector can extract the Bash command (BUG-EVENT1)."
        )
        assert "git log --oneline -3" in content

    @pytest.mark.asyncio
    async def test_capture_tool_use_payload_parses_back_via_detector(self) -> None:
        """Emitted content round-trips through the cheating-attempt detector.

        Cross-cutting integration check: the content this adapter emits must be
        parseable by `run_metrics._extract_bash_command` and recognised as a
        cheating signature by `_is_cheating_command`.
        """
        # Given: an adapter and captured options kwargs.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        from experiments.shared.scripts.run_metrics import (
            _extract_bash_command,
            _is_cheating_command,
        )

        tool_queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=tool_queue,
                container_session=None,
            )

        capture_tool_use = _extract_capture_tool_use_hook(captured)

        # When: a cheating-signature Bash command flows through the hook.
        hook_input = MockHookInput(
            tool_name="Bash",
            tool_input={"command": "git log --oneline -3"},
        )
        await capture_tool_use(hook_input, None, MagicMock())
        content, _, _ = tool_queue.get_nowait()

        # Then: the detector recovers the exact command and counts it as cheating.
        assert _extract_bash_command(content) == "git log --oneline -3"
        assert _is_cheating_command("git log --oneline -3") is True

    @pytest.mark.asyncio
    async def test_capture_tool_use_rewrites_host_paths_for_events(
        self,
        tmp_path: Path,
    ) -> None:
        """Tool-use event payloads hide translated host mirror paths."""
        # Given: a container session and a captured PostToolUse hook.
        adapter = ClaudeAgentSDKAdapter(SDKAdapterConfig())
        session = _container_session(tmp_path)
        captured: dict[str, object] = {}

        def _fake_options(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        tool_queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()
        with patch.object(claude_sdk_adapter, "ClaudeAgentOptions", _fake_options):
            adapter._build_options(
                working_dir="/work",
                tool_queue=tool_queue,
                container_session=session,
            )

        capture_tool_use = _extract_capture_tool_use_hook(captured)
        host_path = tmp_path / "testcase" / "base_commit_hash"

        # When: the hook sees the host path after input translation.
        hook_input = MockHookInput(
            tool_name="Read",
            tool_input={"file_path": str(host_path)},
        )
        await capture_tool_use(hook_input, None, MagicMock())

        # Then: the queued event content uses the canonical container path.
        content, output_type, tool_name = tool_queue.get_nowait()
        assert output_type == "tool_use"
        assert tool_name == "Read"
        assert "/testcase/base_commit_hash" in content
        assert str(tmp_path) not in content
