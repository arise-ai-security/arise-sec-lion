"""Tests for OpenHandsAdapter."""

import logging
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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


def _adapter(**kwargs: Any) -> OpenHandsAdapter:
    return OpenHandsAdapter(model="openai/gpt-4o", **kwargs)


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


class _ThreadRecordingConversation(_FakeConversation):
    def __init__(self) -> None:
        super().__init__()
        self.send_thread_name: str | None = None
        self.run_thread_name: str | None = None
        self.run_saw_message = False

    def send_message(self, message: str) -> None:
        self.send_thread_name = threading.current_thread().name
        super().send_message(message)

    def run(self) -> None:
        self.run_thread_name = threading.current_thread().name
        self.run_saw_message = self.messages == ["Threaded task"]
        super().run()


class _SlowSendMessageConversation(_FakeConversation):
    def __init__(self) -> None:
        super().__init__()
        self._gate = threading.Event()
        self.run_called = False

    def send_message(self, message: str) -> None:
        self.messages.append(message)
        self._gate.wait(timeout=30)

    def run(self) -> None:
        self.run_called = True
        super().run()

    def release(self) -> None:
        self._gate.set()


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
    def test_build_native_tools_returns_configured_openhands_tools(self) -> None:
        # Given: an explicit OpenHands native tool allowlist.
        adapter = _adapter(allowed_tools=["file_editor", "glob", "grep"])

        # When: building SDK Tool specs.
        tools = adapter._build_native_tools(lambda **kw: kw)

        # Then: only the configured native tool names are returned.
        assert tools == [
            {"name": "file_editor"},
            {"name": "glob"},
            {"name": "grep"},
        ]

    def test_build_native_tools_rejects_mcp_or_unknown_tool_names(self) -> None:
        # Given: an MCP tool name accidentally placed in the native allowlist.
        adapter = _adapter(allowed_tools=["file_editor", "valgrind_run"])

        # When/Then: native tool validation rejects it before SDK construction.
        with pytest.raises(ValueError, match="Unsupported OpenHands native tool"):
            adapter._build_native_tools(lambda **kw: kw)

    def test_action_and_observation_events_are_tool_events(self) -> None:
        adapter = _adapter()
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
        adapter = _adapter()
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
        adapter = _adapter(timeout_seconds=1)
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
    async def test_conversation_message_and_run_share_blocking_thread(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """Run the dependent OpenHands calls together before reading SDK events."""

        # Given: a conversation that records where send_message and run execute.
        adapter = _adapter(timeout_seconds=1)
        conversation = _ThreadRecordingConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args: conversation,
        )

        # When: the adapter executes a worker session.
        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Threaded task",
                "agent_id": uuid4(),
                "working_directory": str(tmp_path),
            }
        ):
            events.append(event)

        # Then: both SDK lifecycle calls ran on the same OpenHands worker thread.
        assert conversation.send_thread_name is not None
        assert conversation.run_thread_name is not None
        assert conversation.send_thread_name.startswith("openhands")
        assert conversation.run_thread_name == conversation.send_thread_name

        # And: run observed the message before domain events were extracted.
        assert conversation.run_saw_message is True
        assert any(isinstance(event, ThoughtCaptured) for event in events)
        assert isinstance(events[-1], WorkCompleted)

    @pytest.mark.asyncio
    async def test_timeout_covers_blocking_send_message(
        self,
        monkeypatch,
        caplog,
        tmp_path: Path,
    ) -> None:
        """A hang before run() still hits the adapter timeout and cleanup path."""

        # Given: send_message blocks before OpenHands can enter run().
        monkeypatch.setattr(
            "infrastructure.adapters.worker.openhands_adapter._SHUTDOWN_GRACE_SECONDS",
            0.05,
        )
        adapter = _adapter(timeout_seconds=0.01)
        conversation = _SlowSendMessageConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args: conversation,
        )

        # When: the adapter executes the session.
        events = []
        with caplog.at_level(logging.WARNING):
            async for event in adapter.run_session(
                {
                    "task_description": "Blocked before run",
                    "agent_id": uuid4(),
                    "working_directory": str(tmp_path),
                }
            ):
                events.append(event)

        # Then: the call times out, requests SDK cleanup, and does not run.
        assert conversation.paused is True
        assert conversation.closed is True
        assert conversation.run_called is False
        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason
        assert any("did not exit" in record.message for record in caplog.records)

        conversation.release()

    @pytest.mark.asyncio
    async def test_timeout_shutdown_uses_current_child_pid_diff(
        self,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """MCP children spawned during send_message/run are included in shutdown."""

        # Given: child PID snapshots where an MCP child appears after start.
        monkeypatch.setattr(
            "infrastructure.adapters.worker.openhands_adapter._SHUTDOWN_GRACE_SECONDS",
            0.05,
        )
        snapshots = iter([{100}, {100, 200}, {100, 200}])
        monkeypatch.setattr(
            "infrastructure.adapters.worker.openhands_adapter._snapshot_child_pids",
            lambda: next(snapshots, {100, 200}),
        )
        adapter = _adapter(timeout_seconds=0.01)
        conversation = _StuckConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args: conversation,
        )
        captured_pid_lists: list[list[int] | None] = []

        def _record_shutdown(_conversation: object, *, mcp_child_pids: list[int] | None) -> None:
            captured_pid_lists.append(mcp_child_pids)

        monkeypatch.setattr(adapter, "_request_shutdown", _record_shutdown)

        # When: the run times out.
        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Spawned MCP child",
                "agent_id": uuid4(),
                "working_directory": str(tmp_path),
            }
        ):
            events.append(event)

        # Then: shutdown receives the child created after the baseline snapshot once.
        assert isinstance(events[-1], WorkFailed)
        assert captured_pid_lists == [[200]]

        conversation.release()

    @pytest.mark.asyncio
    async def test_timeout_requests_sdk_shutdown(self, monkeypatch, tmp_path: Path) -> None:
        adapter = _adapter(timeout_seconds=0.01)
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
        adapter = _adapter(timeout_seconds=0.01)
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
    """Verify ``task_context['mcp_servers']`` reaches the OpenHands Agent config."""

    def test_extract_mcp_servers_returns_none_when_absent(self) -> None:
        # Given/When/Then
        adapter = _adapter()
        assert adapter._extract_mcp_servers({}) is None
        assert adapter._extract_mcp_servers({"mcp_servers": {}}) is None

    def test_extract_mcp_servers_returns_raw_dict_when_present(self) -> None:
        # Given
        adapter = _adapter()
        raw = {"security_tools": {"command": "python", "args": [], "env": {}}}

        # When
        result = adapter._extract_mcp_servers({"mcp_servers": raw})

        # Then
        assert result == raw

    def test_build_conversation_passes_mcp_config_kwarg_to_agent(
        self,
    ) -> None:
        """Agent receives the MCP config via its dedicated ``mcp_config`` kwarg.

        Regression: openhands-sdk 1.20's ``Agent.tools`` is a typed list of
        ``Tool`` instances and rejects ``MCPToolDefinition`` with a Pydantic
        ``model_type`` error. MCP servers must be wired through the
        ``mcp_config`` kwarg instead — ``Agent`` builds the in-process MCP
        bridge from there.
        """
        # Given: a minimal adapter and stubbed openhands.sdk surface.
        adapter = OpenHandsAdapter(
            model="openai/gpt-4o",
            api_key="sk-test",
            allowed_tools=["file_editor", "glob", "grep"],
            mcp_tools=["shell_in_container", "valgrind_run", "klee_run"],
        )
        captured: dict[str, object] = {}

        def _fake_agent(*, llm, tools, mcp_config, include_default_tools):
            captured["llm"] = llm
            captured["tools"] = list(tools)
            captured["mcp_config"] = mcp_config
            captured["include_default_tools"] = include_default_tools
            return "agent-stub"

        # The other openhands SDK pieces are imported inside _build_conversation;
        # we stub the entire openhands.sdk module surface used here.
        import sys
        from unittest.mock import MagicMock

        fake_sdk = MagicMock()
        fake_sdk.LLM = MagicMock(return_value="llm-stub")
        fake_sdk.Agent = MagicMock(side_effect=_fake_agent)
        fake_sdk.Conversation = MagicMock(return_value="conversation-stub")
        fake_sdk.Tool = MagicMock(side_effect=lambda **kw: ("Tool", kw))
        fake_file_editor = MagicMock()
        fake_file_editor.FileEditorTool = MagicMock(name="FileEditorTool")
        fake_file_editor.FileEditorTool.name = "file_editor"
        fake_glob = MagicMock()
        fake_glob.GlobTool = MagicMock(name="GlobTool")
        fake_glob.GlobTool.name = "glob"
        fake_grep = MagicMock()
        fake_grep.GrepTool = MagicMock(name="GrepTool")
        fake_grep.GrepTool.name = "grep"

        previous = {
            name: sys.modules.get(name)
            for name in (
                "openhands.sdk",
                "openhands.tools.file_editor",
                "openhands.tools.glob",
                "openhands.tools.grep",
            )
        }
        sys.modules["openhands.sdk"] = fake_sdk
        sys.modules["openhands.tools.file_editor"] = fake_file_editor
        sys.modules["openhands.tools.glob"] = fake_glob
        sys.modules["openhands.tools.grep"] = fake_grep
        try:
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

        # Then: Agent received the MCP config via the dedicated kwarg, NOT in tools.
        assert "mcp_config" in captured
        mcp_config = captured["mcp_config"]
        assert isinstance(mcp_config, dict)
        assert "mcpServers" in mcp_config
        assert "security_tools" in mcp_config["mcpServers"]
        assert mcp_config["mcpServers"]["security_tools"]["command"] == "python"
        assert captured["include_default_tools"] == ["FinishTool", "ThinkTool"]
        # And: the tools list does NOT contain the MCP server entries.
        for tool in captured["tools"]:
            tool_repr = repr(tool)
            assert "security_tools" not in tool_repr
            assert "MCPToolDefinition" not in tool_repr
        # And: the conversation handle came back from the fake SDK.
        assert conversation == "conversation-stub"


class TestTerminalToolRegistration:
    """E.10: OpenHands worker locality enforcement.

    The host-side ``TerminalTool`` is intentionally NOT registered on the
    OpenHands agent. Container shells go through the MCP
    ``shell_in_container`` tool instead, so build/test commands never run
    on the host where ``/src`` and ``/testcase`` do not exist.
    """

    def test_no_terminaltool_registered(self) -> None:
        """``_build_conversation`` produces a tools list with no host terminal tool."""
        # Given: a minimal adapter and stubs for every openhands SDK piece.
        adapter = OpenHandsAdapter(
            model="openai/gpt-4o",
            api_key="sk-test",
            allowed_tools=["file_editor", "glob", "grep"],
        )

        import sys
        from unittest.mock import MagicMock

        # ``Tool`` is captured as a tagged tuple so we can inspect the kwargs
        # the adapter passed without depending on the real SDK shape.
        def _tool_factory(**kw):
            return ("Tool", kw)

        fake_sdk = MagicMock()
        fake_sdk.LLM = MagicMock(return_value="llm-stub")
        fake_sdk.Agent = MagicMock(return_value="agent-stub")

        captured_tools: list[Any] = []

        def _conversation_factory(*_args, **kwargs):
            # ``tools`` is set on the Agent, but the adapter passes the
            # composed agent to Conversation. We snapshot the list at
            # Agent construction time below.
            return ("Conversation", kwargs)

        fake_sdk.Conversation = MagicMock(side_effect=_conversation_factory)
        fake_sdk.Tool = MagicMock(side_effect=_tool_factory)

        def _agent_factory(**kwargs):
            captured_tools.extend(kwargs.get("tools", []))
            return "agent-stub"

        fake_sdk.Agent = MagicMock(side_effect=_agent_factory)

        fake_file_editor = MagicMock()
        fake_file_editor.FileEditorTool = MagicMock(name="FileEditorTool")
        fake_file_editor.FileEditorTool.name = "file_editor"
        fake_glob = MagicMock()
        fake_glob.GlobTool = MagicMock(name="GlobTool")
        fake_glob.GlobTool.name = "glob"
        fake_grep = MagicMock()
        fake_grep.GrepTool = MagicMock(name="GrepTool")
        fake_grep.GrepTool.name = "grep"

        previous = {
            name: sys.modules.get(name)
            for name in (
                "openhands.sdk",
                "openhands.tools.file_editor",
                "openhands.tools.glob",
                "openhands.tools.grep",
            )
        }
        sys.modules["openhands.sdk"] = fake_sdk
        sys.modules["openhands.tools.file_editor"] = fake_file_editor
        sys.modules["openhands.tools.glob"] = fake_glob
        sys.modules["openhands.tools.grep"] = fake_grep
        try:
            adapter._build_conversation(working_dir="/work")
        finally:
            for name, mod in previous.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod

        # Then: no Tool was registered for the OpenHands host terminal,
        # under any of the common SDK names. Compare against a small
        # allowlist of canonical aliases the SDK has used so this test
        # stays robust across SDK versions.
        terminal_aliases = {
            "execute_bash",  # current SDK canonical name
            "TerminalTool",
            "terminal",
            "bash",
        }
        for tool in captured_tools:
            if isinstance(tool, tuple) and tool[0] == "Tool":
                name = tool[1].get("name")
                assert name not in terminal_aliases, (
                    f"E.10 violated: host terminal tool {name!r} still "
                    "registered. Shell access must flow through the MCP "
                    "`shell_in_container` tool instead."
                )

        # And: the file editor tool IS registered (bind-mounted host paths
        # are transparent inside the container; the file tool itself is
        # locality-safe).
        registered_names = [
            tool[1].get("name")
            for tool in captured_tools
            if isinstance(tool, tuple) and tool[0] == "Tool"
        ]
        assert "file_editor" in registered_names
        assert "glob" in registered_names
        assert "grep" in registered_names


class TestMcpChildProcessLifecycle:
    """G.4: verify ``_request_shutdown`` reaps surviving MCP stdio subprocesses.

    The OpenHands SDK boots MCP servers as long-lived stdio subprocesses.
    A graceful ``Conversation.close()`` is best-effort and has been
    observed to leave ``python -m plugins.security.mcp.security_tools_server``
    children alive on shutdown, so the adapter wires a SIGTERM reaper that
    captures child PIDs immediately after ``_build_conversation`` and
    terminates survivors after ``close()`` runs.

    Booting the real ``Conversation`` just to assert MCP reaping is heavier
    than the test merits — instead we exercise the reaper directly:
    spawn a real ``cat`` subprocess (a stand-in for the MCP server), pass
    its PID into ``_request_shutdown`` as a "claimed MCP child," and
    assert the reaper SIGTERMs it. ``psutil`` is a test-only dependency.
    """

    def test_close_terminates_mcp_subprocesses(self) -> None:
        # Skip when psutil is not available — it is a test-only dependency.
        psutil = pytest.importorskip("psutil")

        import subprocess
        import time as _time
        from shutil import which

        # Given: a long-running stdio subprocess that stands in for an MCP
        # server (``cat`` blocks on stdin so it stays alive until killed).
        cat = which("cat")
        if cat is None:
            pytest.skip("cat executable not available")
        child = subprocess.Popen(  # noqa: S603 - test-owned executable path.
            [cat],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert psutil.pid_exists(child.pid), "test scaffold failed to spawn child"

            # And: a conversation that DOES NOT terminate the child on
            # close (this is the failure mode the reaper exists to handle).
            class _FakeConv:
                def __init__(self) -> None:
                    self.paused = False
                    self.closed = False

                def pause(self) -> None:
                    self.paused = True

                def close(self) -> None:
                    # Intentionally a no-op WRT the child — mirrors the
                    # SDK behavior the reaper compensates for.
                    self.closed = True

            adapter = _adapter()
            conversation = _FakeConv()

            # When: ``_request_shutdown`` is called with the child PID
            # marked as a tracked MCP subprocess.
            adapter._request_shutdown(conversation, mcp_child_pids=[child.pid])

            # Then: the SDK-native cleanup path ran.
            assert conversation.paused is True
            assert conversation.closed is True

            # And: the reaper SIGTERMed the child; it exits shortly after.
            deadline = _time.monotonic() + 5.0
            while _time.monotonic() < deadline and psutil.pid_exists(child.pid):
                try:
                    child.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue

            assert not psutil.pid_exists(child.pid), (
                f"MCP child PID {child.pid} survived _request_shutdown — "
                "the G.4 reaper is not running. Long-running matrix runs "
                "will accumulate orphan MCP stdio subprocesses."
            )
        finally:
            # Defensive: never leak the child if an assertion above failed.
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2.0)

    def test_request_shutdown_skips_reaper_when_no_pids(self) -> None:
        """The reaper short-circuits when no MCP child PIDs were captured.

        Workers without MCP servers (or test code that bypasses the
        snapshot) must still get full SDK-native cleanup without the
        reaper attempting to kill arbitrary PIDs.
        """
        # Given: a fake conversation with no claimed MCP children.
        class _FakeConv:
            def __init__(self) -> None:
                self.paused = False
                self.closed = False

            def pause(self) -> None:
                self.paused = True

            def close(self) -> None:
                self.closed = True

        adapter = _adapter()
        conversation = _FakeConv()

        # When: shutdown runs with no PID list.
        adapter._request_shutdown(conversation, mcp_child_pids=None)
        adapter._request_shutdown(conversation, mcp_child_pids=[])

        # Then: pause/close still ran on every invocation.
        assert conversation.paused is True
        assert conversation.closed is True

    def test_request_shutdown_reaps_zombie_mcp_child(self) -> None:
        """Codex review HIGH #2: after SIGTERM, ``waitpid`` MUST run so
        the dead child is reaped (not left as a zombie).

        Previously ``_request_shutdown`` sent SIGTERM and returned. An
        MCP child that exited immediately stayed as a ``Z`` (zombie)
        entry in the kernel until the parent process exited; long
        matrix runs accumulated dozens of zombies per session.
        """
        import subprocess as _subprocess

        # Given: a short-lived child that exits within ~0.2s on its own.
        # The child must be a direct child of this process so ``waitpid``
        # can reap it from inside the adapter.
        child = _subprocess.Popen(  # noqa: S603 - test-owned Python command.
            [sys.executable, "-c", "import time; time.sleep(0.1)"],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
        )
        try:
            class _FakeConv:
                def pause(self) -> None: ...
                def close(self) -> None: ...

            adapter = _adapter()
            # Let the child exit on its own before we run the reaper —
            # this is the zombie case. SIGTERM to an already-exited PID
            # is harmless; the test is that waitpid() actually runs.
            time.sleep(0.3)

            # When: the reaper runs.
            adapter._request_shutdown(_FakeConv(), mcp_child_pids=[child.pid])

            # Then: the kernel no longer carries a zombie for the PID.
            # If the adapter did NOT call waitpid, ``waitpid(WNOHANG)``
            # here would return ``(pid, status)`` and the kernel would
            # still hold the entry. After the reaper, the table entry
            # is gone and a second waitpid raises ``ChildProcessError``.
            import os as _os

            with pytest.raises(ChildProcessError):
                _os.waitpid(child.pid, _os.WNOHANG)
        finally:
            # Defensive: if the test failed before the reaper ran,
            # subprocess.Popen.__del__ would warn about leaks. Reap
            # ourselves to keep the test output clean.
            try:
                child.wait(timeout=2.0)
            except _subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2.0)
            except ChildProcessError:
                pass

    def test_request_shutdown_sigkills_unresponsive_mcp_child(self) -> None:
        """Codex review HIGH #2: SIGTERM-ignoring children must be
        escalated to SIGKILL within ``grace_seconds + a small margin``.

        A real MCP child that traps SIGTERM (or is stuck in a tight
        loop) would otherwise stay alive forever; the original reaper
        had no escalation path.
        """
        import os as _os
        import subprocess as _subprocess

        psutil = pytest.importorskip("psutil")

        # Given: a child that installs a no-op SIGTERM handler and then
        # sleeps for a long time. ``signal.signal(SIGTERM, lambda...)``
        # makes the kernel deliver the signal but the handler ignores
        # it, so SIGTERM alone cannot kill the process.
        child = _subprocess.Popen(  # noqa: S603 - test-owned Python command.
            [
                sys.executable,
                "-c",
                (
                    "import signal, time; "
                    "signal.signal(signal.SIGTERM, lambda s, f: None); "
                    "time.sleep(60)"
                ),
            ],
            stdin=_subprocess.PIPE,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
        )
        try:
            # Give the child a moment to install its signal handler so
            # the SIGTERM we send is actually ignored (not delivered to
            # a process that hasn't reached the signal.signal call yet).
            time.sleep(0.5)
            assert psutil.pid_exists(child.pid), (
                "test scaffold: SIGTERM-ignoring child failed to spawn"
            )

            class _FakeConv:
                def pause(self) -> None: ...
                def close(self) -> None: ...

            adapter = _adapter()

            # When: the reaper runs with a tight grace so the test is
            # fast. Internal helper to bypass the constant default.
            started = time.monotonic()
            adapter._reap_with_escalation(
                {child.pid}, grace_seconds=0.5, poll_interval=0.05
            )
            elapsed = time.monotonic() - started

            # Then: the child is gone within ~grace + a small slack.
            # 1.5s is generous — escalation is grace (0.5) + step-3
            # SIGKILL loop (another ~0.5 worst case) + slack.
            assert elapsed < 1.5, (
                f"SIGKILL escalation took {elapsed:.2f}s — too slow. "
                "The reaper must fall back to SIGKILL after the grace "
                "period for SIGTERM-ignoring MCP children."
            )

            # And: a second ``waitpid`` confirms the PID is reaped.
            with pytest.raises(ChildProcessError):
                _os.waitpid(child.pid, _os.WNOHANG)
        finally:
            if child.poll() is None:
                child.kill()
                with suppress(_subprocess.TimeoutExpired):
                    child.wait(timeout=2.0)

    def test_reap_with_escalation_handles_empty_set(self) -> None:
        """Defensive: empty PID set is a no-op (mirrors the no-PIDs
        contract of ``_request_shutdown``)."""
        adapter = _adapter()
        # Should not raise nor block.
        adapter._reap_with_escalation(set(), grace_seconds=0.1, poll_interval=0.05)

    def test_reap_with_escalation_tolerates_already_reaped_pids(self) -> None:
        """``ChildProcessError`` (already reaped) and ``ProcessLookupError``
        (already gone) must NOT propagate — the reaper is best-effort."""
        import os as _os

        # Given: an arbitrary PID that is not a child of this process.
        # ``os.kill(pid, 0)`` will succeed (process exists) but
        # ``waitpid`` will raise ``ChildProcessError`` because the
        # kernel only allows waiting for direct children.
        adapter = _adapter()

        with (
            patch("infrastructure.adapters.worker.openhands_adapter.os.kill"),
            patch(
                "infrastructure.adapters.worker.openhands_adapter.os.waitpid",
                side_effect=ChildProcessError(),
            ),
            patch(
                "infrastructure.adapters.worker.openhands_adapter._pid_alive",
                return_value=True,
            ),
        ):
            adapter._reap_with_escalation(
                {_os.getpid()},  # never the current process
                grace_seconds=0.1,
                poll_interval=0.05,
            )
            # Reached here without raising — pass.
