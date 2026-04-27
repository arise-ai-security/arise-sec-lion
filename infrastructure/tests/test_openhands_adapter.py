"""Tests for OpenHandsAdapter."""

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from openhands.sdk.conversation.conversation_stats import ConversationStats
from openhands.sdk.llm.utils.metrics import Metrics, ResponseLatency, TokenUsage

from core.domain.events.events import (
    ThoughtCaptured,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter


def _mock_event(
    *,
    class_name: str,
    message: str | None = None,
    content: str | None = None,
):
    event_type = type(class_name, (), {})
    event = event_type()
    event.message = message
    event.content = content
    return event


class _FakeConversation:
    def __init__(self) -> None:
        self.state = SimpleNamespace(events=[])
        metrics = Metrics(
            model_name="openai/gpt-4o",
            accumulated_cost=0.12,
            accumulated_token_usage=TokenUsage(
                model="openai/gpt-4o",
                prompt_tokens=10,
                completion_tokens=5,
                cache_read_tokens=2,
                cache_write_tokens=1,
                reasoning_tokens=3,
                context_window=4096,
                per_turn_token=15,
                response_id="resp-1",
            ),
            costs=[
                {"model": "openai/gpt-4o", "cost": 0.04, "timestamp": 100.0},
                {"model": "openai/gpt-4o-mini", "cost": 0.08, "timestamp": 101.0},
            ],
            response_latencies=[
                ResponseLatency(model="openai/gpt-4o", latency=0.4, response_id="resp-1"),
            ],
            token_usages=[
                TokenUsage(
                    model="openai/gpt-4o",
                    prompt_tokens=10,
                    completion_tokens=5,
                    cache_read_tokens=2,
                    cache_write_tokens=1,
                    reasoning_tokens=3,
                    context_window=4096,
                    per_turn_token=15,
                    response_id="resp-1",
                ),
            ],
        )
        self.conversation_stats = ConversationStats(usage_to_metrics={"primary": metrics})
        self.messages: list[str] = []
        self.paused = False
        self.closed = False

    def send_message(self, message: str) -> None:
        self.messages.append(message)

    def run(self) -> None:
        self.state.events.extend(
            [
                _mock_event(class_name="MessageEvent", content="first output"),
                _mock_event(class_name="ActionEvent", content="thinking"),
                _mock_event(class_name="MessageEvent", content="final output"),
            ]
        )

    def pause(self) -> None:
        self.paused = True

    def close(self) -> None:
        self.closed = True


class _SlowConversation(_FakeConversation):
    def run(self) -> None:
        time.sleep(0.05)


class _StuckConversation(_FakeConversation):
    """Conversation whose run() ignores pause/close and blocks until released."""

    def __init__(self) -> None:
        super().__init__()
        self._gate = threading.Event()

    def run(self) -> None:
        self._gate.wait(timeout=30)

    def release(self) -> None:
        self._gate.set()


class TestOpenHandsAdapter:
    def test_tool_policy_blocks_file_editor_when_write_is_denied(self) -> None:
        adapter = OpenHandsAdapter(allowed_tools=["*"], disallowed_tools=["Write"])

        assert adapter._file_editor_allowed() is False
        assert adapter._tool_allowed("Bash") is True

    def test_action_and_observation_events_are_tool_events(self) -> None:
        adapter = OpenHandsAdapter()
        action = SimpleNamespace(command="echo hi")
        observation = SimpleNamespace(
            command="echo hi",
            metadata=SimpleNamespace(exit_code=0),
            content=[SimpleNamespace(text="hi")],
        )

        assert adapter._classify_event(SimpleNamespace(action=action)) == "tool_use"
        assert adapter._classify_event(SimpleNamespace(observation=observation)) == "tool_result"
        action_content = adapter._extract_event_content(SimpleNamespace(action=action))
        assert action_content is not None
        assert "Tool: SimpleNamespace" in action_content
        assert "Tool result:" in adapter._extract_event_content(
            SimpleNamespace(observation=observation)
        )

    def test_extract_result_compacts_observations_before_core(self, tmp_path: Path) -> None:
        adapter = OpenHandsAdapter()
        observation = SimpleNamespace(
            command="make test",
            metadata=SimpleNamespace(exit_code=1),
            content=[SimpleNamespace(text="failure output\nstack trace")],
        )
        event = SimpleNamespace(observation=observation)
        conversation = SimpleNamespace(state=SimpleNamespace(events=[event]))

        result = adapter._extract_result(conversation, str(tmp_path))

        assert result == "$ make test (exit 1)\nfailure output\nstack trace"

    @pytest.mark.asyncio
    async def test_successful_execution_uses_conversation_output(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        adapter = OpenHandsAdapter(timeout_seconds=1)
        conversation = _FakeConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args: conversation,
        )

        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Implement feature",
                "agent_id": uuid4(),
                "working_directory": str(tmp_path),
            }
        ):
            events.append(event)

        assert conversation.messages == ["Implement feature"]
        assert conversation.paused is True
        assert conversation.closed is True
        assert any(isinstance(event, ThoughtCaptured) for event in events)
        cost_event = next(event for event in events if isinstance(event, WorkerCostRecorded))
        assert cost_event.cost_usd == pytest.approx(0.12)
        assert cost_event.tokens == 21
        assert cost_event.prompt_tokens == 10
        assert cost_event.completion_tokens == 5
        assert cost_event.cache_read_tokens == 2
        assert cost_event.cache_write_tokens == 1
        assert cost_event.reasoning_tokens == 3
        assert cost_event.model == "openai/gpt-4o"
        assert len(cost_event.usage_metrics) == 1
        assert cost_event.usage_metrics[0].usage_id == "primary"
        assert len(cost_event.usage_metrics[0].cost_items) == 2
        assert cost_event.usage_metrics[0].cost_items[1].model == "openai/gpt-4o-mini"
        assert isinstance(events[-1], WorkCompleted)
        assert "final output" in events[-1].result

    @pytest.mark.asyncio
    async def test_timeout_requests_sdk_shutdown(self, monkeypatch, tmp_path: Path) -> None:
        adapter = OpenHandsAdapter(timeout_seconds=0.01)
        conversation = _SlowConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args: conversation,
        )

        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Long task",
                "agent_id": uuid4(),
                "working_directory": str(tmp_path),
            }
        ):
            events.append(event)

        assert conversation.paused is True
        assert conversation.closed is True
        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason

    @pytest.mark.asyncio
    async def test_timeout_logs_warning_when_thread_outlives_grace_period(
        self,
        monkeypatch,
        caplog,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "infrastructure.adapters.worker.openhands_adapter._SHUTDOWN_GRACE_SECONDS",
            0.05,
        )
        adapter = OpenHandsAdapter(timeout_seconds=0.01)
        conversation = _StuckConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args: conversation,
        )

        events = []
        with caplog.at_level(logging.WARNING):
            async for event in adapter.run_session(
                {
                    "task_description": "Stuck task",
                    "agent_id": uuid4(),
                    "working_directory": str(tmp_path),
                }
            ):
                events.append(event)

        assert isinstance(events[-1], WorkFailed)
        assert any("did not exit" in record.message for record in caplog.records)

        conversation.release()


class TestMCPServersWiring:
    """Verify ``task_context['mcp_servers']`` reaches ``create_mcp_tools``."""

    def test_extract_mcp_servers_returns_none_when_absent(self) -> None:
        # Given/When/Then
        adapter = OpenHandsAdapter()
        assert adapter._extract_mcp_servers({}) is None
        assert adapter._extract_mcp_servers({"mcp_servers": {}}) is None

    def test_extract_mcp_servers_returns_raw_dict_when_present(self) -> None:
        # Given
        adapter = OpenHandsAdapter()
        raw = {"security_tools": {"command": "python", "args": [], "env": {}}}

        # When
        result = adapter._extract_mcp_servers({"mcp_servers": raw})

        # Then
        assert result == raw

    def test_build_conversation_calls_create_mcp_tools_with_camelcase_envelope(
        self,
    ) -> None:
        """create_mcp_tools is called with ``{"mcpServers": {...}}``."""
        # Given: a minimal adapter and a fake create_mcp_tools.
        adapter = OpenHandsAdapter(model="openai/gpt-4o", api_key="sk-test")
        captured: dict[str, object] = {}

        def _fake_create_mcp_tools(config, *_args, **_kwargs):
            captured["config"] = config
            return ["mcp-tool-stub-1", "mcp-tool-stub-2"]

        # The other openhands SDK pieces are imported inside _build_conversation;
        # we stub the entire openhands.sdk module surface used here.
        import sys
        from unittest.mock import MagicMock

        fake_sdk = MagicMock()
        fake_sdk.LLM = MagicMock(return_value="llm-stub")
        fake_sdk.Agent = MagicMock(return_value="agent-stub")
        fake_sdk.Conversation = MagicMock(return_value="conversation-stub")
        fake_sdk.Tool = MagicMock(side_effect=lambda **kw: ("Tool", kw))
        fake_mcp = MagicMock()
        fake_mcp.create_mcp_tools = _fake_create_mcp_tools
        fake_file_editor = MagicMock()
        fake_file_editor.FileEditorTool = MagicMock(name="FileEditorTool")
        fake_file_editor.FileEditorTool.name = "file_editor"
        fake_terminal = MagicMock()
        fake_terminal.TerminalTool = MagicMock(name="TerminalTool")
        fake_terminal.TerminalTool.name = "execute_bash"

        previous = {
            name: sys.modules.get(name)
            for name in (
                "openhands.sdk",
                "openhands.sdk.mcp",
                "openhands.tools.file_editor",
                "openhands.tools.terminal",
            )
        }
        sys.modules["openhands.sdk"] = fake_sdk
        sys.modules["openhands.sdk.mcp"] = fake_mcp
        sys.modules["openhands.tools.file_editor"] = fake_file_editor
        sys.modules["openhands.tools.terminal"] = fake_terminal
        try:
            from infrastructure.adapters.worker import openhands_adapter

            with patch.object(
                openhands_adapter,
                "_patch_openhands_fn_converter",
                lambda: None,
            ):
                conversation = adapter._build_conversation(
                    working_dir="/work",
                    mcp_servers={
                        "security_tools": {
                            "command": "python",
                            "args": ["-m", "plugins.security.mcp.security_tools_server"],
                            "env": {"ARISE_SECBENCH_CONTAINER_ID": "abc"},
                        }
                    },
                )
        finally:
            for name, mod in previous.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod

        # Then: create_mcp_tools received the camelCase MCPConfig envelope.
        assert "config" in captured
        config = captured["config"]
        assert isinstance(config, dict)
        assert "mcpServers" in config
        assert "security_tools" in config["mcpServers"]
        assert config["mcpServers"]["security_tools"]["command"] == "python"
        # And: the conversation handle came back from the fake SDK.
        assert conversation == "conversation-stub"
