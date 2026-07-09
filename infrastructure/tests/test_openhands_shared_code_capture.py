"""OpenHands adapter capture of file views/edits into the shared code context.

Verification point #1 (confirmed in code): the OpenHands ``FileEditorObservation``
for a ``view`` carries the returned file content (in ``observation.content`` /
``.text``; full file up to the SDK's 16K cap, ``cat -n`` framed). The adapter
intercepts those observations and forwards them to the shared-code port:

* ``view``  -> ``record_view(root_id, agent_id, path, content)``
* ``str_replace`` / ``create`` -> ``record_edit(root_id, agent_id, path)``

Capture is collision-free with the live worker aggregate: the port (bound by the
adapter) routes built events back into the worker stream so the orchestrator's
single in-memory aggregate re-sequences them — never an out-of-band append.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.domain.events.events import DomainEvent, SourceFileEdited, SourceFileObserved
from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter
from infrastructure.tests.test_openhands_adapter import _FakeConversation


def _adapter(**kwargs: Any) -> OpenHandsAdapter:
    return OpenHandsAdapter(model="openai/gpt-4o", **kwargs)


class _RecordingSharedCodePort:
    """Captures record_view/record_edit calls and routes built events to the sink.

    Mirrors the bound-capture provider the real wiring supplies: emission is routed
    through the adapter-supplied ``emit_sink`` (which enqueues for the worker
    stream) rather than persisted out-of-band.
    """

    def __init__(self) -> None:
        self.views: list[tuple[UUID, UUID, str, str]] = []
        self.edits: list[tuple[UUID, UUID, str]] = []
        self._sink: Any = None

    async def record_view(self, root_id: UUID, agent_id: UUID, path: str, content: str) -> None:
        self.views.append((root_id, agent_id, path, content))
        await self._sink(
            agent_id,
            [
                SourceFileObserved(
                    aggregate_id=agent_id,
                    sequence_number=0,
                    path=path,
                    content=content,
                    content_sha256="x" * 64,
                    observed_by=str(agent_id),
                )
            ],
        )

    async def record_edit(self, root_id: UUID, agent_id: UUID, path: str) -> None:
        self.edits.append((root_id, agent_id, path))
        await self._sink(
            agent_id,
            [
                SourceFileEdited(
                    aggregate_id=agent_id,
                    sequence_number=0,
                    path=path,
                    edited_by=str(agent_id),
                )
            ],
        )

    def bind_capture(self, emit_sink: Any) -> _RecordingSharedCodePort:
        self._sink = emit_sink
        return self

    async def code_block(self, root_id: UUID) -> str | None:  # pragma: no cover - unused here
        return None


def _file_editor_observation(*, command: str, path: str, text: str) -> Any:
    """An SDK-shaped event carrying a FileEditorObservation under ``.observation``."""
    observation = SimpleNamespace(
        command=command,
        path=path,
        content=[SimpleNamespace(text=text)],
        metadata=None,
    )
    return SimpleNamespace(observation=observation, action=None, id=uuid4())


class _FileToolConversation(_FakeConversation):
    """Conversation whose run() emits a view then a str_replace observation."""

    def run(self) -> None:
        self.state.events.extend(
            [
                _file_editor_observation(
                    command="view",
                    path="/src/demo/vuln.c",
                    text="Here's the result of running `cat -n` on /src/demo/vuln.c:\n     1\tint x;",
                ),
                _file_editor_observation(
                    command="str_replace",
                    path="/src/demo/vuln.c",
                    text="The file /src/demo/vuln.c has been edited.",
                ),
            ]
        )


@pytest.mark.asyncio
async def test_view_observation_forwards_to_record_view(monkeypatch, tmp_path) -> None:
    # Given: an adapter wired with a recording shared-code port and a run that
    # performs a file view followed by an edit.
    port = _RecordingSharedCodePort()
    adapter = _adapter(timeout_seconds=1, shared_code_port=port)
    conversation = _FileToolConversation()
    monkeypatch.setattr(adapter, "_build_conversation", lambda *_args: conversation)
    root_id = uuid4()
    agent_id = uuid4()

    # When
    events: list[DomainEvent] = []
    async for event in adapter.run_session(
        {
            "task_description": "Patch the overflow",
            "agent_id": agent_id,
            "working_directory": str(tmp_path),
            "root_id": root_id,
        }
    ):
        events.append(event)

    # Then: the view was forwarded with the full observation content, and the edit
    # was forwarded as record_edit.
    assert len(port.views) == 1
    view_root, view_agent, view_path, view_content = port.views[0]
    assert view_root == root_id
    assert view_agent == agent_id
    assert view_path == "/src/demo/vuln.c"
    assert "int x;" in view_content
    assert port.edits == [(root_id, agent_id, "/src/demo/vuln.c")]


@pytest.mark.asyncio
async def test_captured_events_appear_in_worker_stream(monkeypatch, tmp_path) -> None:
    # Given: the same wired adapter and run
    port = _RecordingSharedCodePort()
    adapter = _adapter(timeout_seconds=1, shared_code_port=port)
    conversation = _FileToolConversation()
    monkeypatch.setattr(adapter, "_build_conversation", lambda *_args: conversation)

    # When
    events: list[DomainEvent] = []
    async for event in adapter.run_session(
        {
            "task_description": "Patch the overflow",
            "agent_id": uuid4(),
            "working_directory": str(tmp_path),
            "root_id": uuid4(),
        }
    ):
        events.append(event)

    # Then: the SourceFile events are yielded INTO the worker stream (so the live
    # aggregate re-sequences them — recoverable from the event store, no OCC race).
    assert any(isinstance(e, SourceFileObserved) for e in events)
    assert any(isinstance(e, SourceFileEdited) for e in events)
    observed = next(e for e in events if isinstance(e, SourceFileObserved))
    assert observed.path == "/src/demo/vuln.c"


@pytest.mark.asyncio
async def test_no_capture_when_port_absent(monkeypatch, tmp_path) -> None:
    # Given: an adapter WITHOUT a shared-code port (feature off)
    adapter = _adapter(timeout_seconds=1)
    conversation = _FileToolConversation()
    monkeypatch.setattr(adapter, "_build_conversation", lambda *_args: conversation)

    # When
    events: list[DomainEvent] = []
    async for event in adapter.run_session(
        {
            "task_description": "Patch the overflow",
            "agent_id": uuid4(),
            "working_directory": str(tmp_path),
            "root_id": uuid4(),
        }
    ):
        events.append(event)

    # Then: no SourceFile events are emitted (byte-identical to today when off)
    assert not any(isinstance(e, (SourceFileObserved, SourceFileEdited)) for e in events)
