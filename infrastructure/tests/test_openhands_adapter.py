"""Tests for OpenHandsAdapter."""

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
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
    def test_extract_result_compacts_observations_before_core(self) -> None:
        adapter = OpenHandsAdapter()
        observation = SimpleNamespace(
            command="make test",
            metadata=SimpleNamespace(exit_code=1),
            content=[SimpleNamespace(text="failure output\nstack trace")],
        )
        event = SimpleNamespace(observation=observation)
        conversation = SimpleNamespace(state=SimpleNamespace(events=[event]))

        result = adapter._extract_result(conversation, "/tmp/workspace")

        assert result == "$ make test (exit 1)\nfailure output\nstack trace"

    @pytest.mark.asyncio
    async def test_successful_execution_uses_conversation_output(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        adapter = OpenHandsAdapter(timeout_seconds=1)
        conversation = _FakeConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args, **_kwargs: conversation,
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
            lambda *_args, **_kwargs: conversation,
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
        self, monkeypatch, caplog, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "infrastructure.adapters.worker.openhands_adapter._SHUTDOWN_GRACE_SECONDS",
            0.05,
        )
        adapter = OpenHandsAdapter(timeout_seconds=0.01)
        conversation = _StuckConversation()
        monkeypatch.setattr(
            adapter, "_build_conversation", lambda *_args, **_kwargs: conversation,
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

    def test_cap_thought_content_under_limit_passes_through(self) -> None:
        """Short content is returned unchanged; was_truncated False; result_bytes accurate."""
        # Given: a short content string well under the cap
        adapter = OpenHandsAdapter()
        content = "short log line"

        # When: cap helper runs
        capped, was_truncated, result_bytes = adapter._cap_thought_content(content)

        # Then: nothing truncated and the byte count matches the original length
        assert capped == content
        assert was_truncated is False
        assert result_bytes == len(content)

    def test_cap_thought_content_over_limit_truncates_and_flags(self) -> None:
        """Content over the 10_240 cap is truncated; was_truncated True; result_bytes original."""
        # Given: a content string 15 000 chars long
        adapter = OpenHandsAdapter()
        content = "x" * 15_000

        # When: cap helper runs
        capped, was_truncated, result_bytes = adapter._cap_thought_content(content)

        # Then: capped length is bounded; metadata reports the pre-truncation size
        assert was_truncated is True
        assert result_bytes == 15_000
        assert len(capped) <= 10_240 + 100  # + truncation marker
        assert capped.endswith("…[TRUNCATED]")

    @pytest.mark.asyncio
    async def test_thought_events_include_truncation_metadata_for_schema_parity(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """ThoughtCaptured events carry was_truncated/result_bytes (Claude SDK parity)."""
        # Given: a conversation that emits a single plain-output message event
        adapter = OpenHandsAdapter(timeout_seconds=1)
        conversation = _FakeConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda *_args, **_kwargs: conversation,
        )

        # When: running the session
        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Feature",
                "agent_id": uuid4(),
                "working_directory": str(tmp_path),
            }
        ):
            events.append(event)

        # Then: every ThoughtCaptured event carries the new truncation fields
        # (default False / accurate result_bytes for under-cap content), and
        # the non-truncation fields (call_id / tool_input_json) stay None for
        # OpenHands because the SDK does not expose them.
        thought_events = [e for e in events if isinstance(e, ThoughtCaptured)]
        assert thought_events, "expected at least one ThoughtCaptured event"
        for event in thought_events:
            assert event.was_truncated is False
            assert event.result_bytes is not None
            assert event.result_bytes == len(event.content)
            assert event.call_id is None
            assert event.tool_input_json is None

    @pytest.mark.asyncio
    async def test_run_session_prefers_task_context_model(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        adapter = OpenHandsAdapter(model="openai/gpt-4o", timeout_seconds=1)
        conversation = _FakeConversation()
        seen: dict[str, str | None] = {}

        def _fake_build_conversation(
            _working_dir: str,
            *,
            model: str,
            api_key: str | None,
        ):
            seen["model"] = model
            seen["api_key"] = api_key
            return conversation

        monkeypatch.setattr(adapter, "_build_conversation", _fake_build_conversation)

        async for _event in adapter.run_session(
            {
                "task_description": "Implement feature",
                "agent_id": uuid4(),
                "working_directory": str(tmp_path),
                "config": {"base": {"model": "openai/gpt-4-turbo"}},
            }
        ):
            pass

        assert seen["model"] == "openai/gpt-4-turbo"
