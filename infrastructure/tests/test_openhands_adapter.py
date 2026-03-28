"""Tests for OpenHandsAdapter."""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.domain.events.events import ThoughtCaptured, WorkCompleted, WorkFailed
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
        self.conversation_stats = {
            "accumulated_cost": 0.12,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }
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


class TestOpenHandsAdapter:
    @pytest.mark.asyncio
    async def test_successful_execution_uses_conversation_output(self, monkeypatch) -> None:
        adapter = OpenHandsAdapter(timeout_seconds=1)
        conversation = _FakeConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda working_dir: conversation,
        )

        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Implement feature",
                "agent_id": uuid4(),
                "working_directory": "/tmp",
            }
        ):
            events.append(event)

        assert conversation.messages == ["Implement feature"]
        assert conversation.paused is True
        assert conversation.closed is True
        assert any(isinstance(event, ThoughtCaptured) for event in events)
        assert isinstance(events[-1], WorkCompleted)
        assert "final output" in events[-1].result

    @pytest.mark.asyncio
    async def test_timeout_requests_sdk_shutdown(self, monkeypatch) -> None:
        adapter = OpenHandsAdapter(timeout_seconds=0.01)
        conversation = _SlowConversation()
        monkeypatch.setattr(
            adapter,
            "_build_conversation",
            lambda working_dir: conversation,
        )

        events = []
        async for event in adapter.run_session(
            {
                "task_description": "Long task",
                "agent_id": uuid4(),
                "working_directory": "/tmp",
            }
        ):
            events.append(event)

        assert conversation.paused is True
        assert conversation.closed is True
        assert isinstance(events[-1], WorkFailed)
        assert "timed out" in events[-1].reason
