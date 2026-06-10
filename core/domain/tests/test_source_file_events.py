"""TDD tests for SourceFileObserved / SourceFileEdited events.

These events back the shared code-prefix cache (spec
docs/superpowers/specs/2026-06-10-shared-code-prefix-cache-design.md). They
persist viewed file contents and edit markers on the observing worker's
aggregate so the canonical code block is recoverable from the event store
alone (HARD CONSTRAINT). Written before wiring is complete, TDD-style.
"""

import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import SourceFileEdited, SourceFileObserved


def _worker() -> AgentSession:
    """A WORKER aggregate ready to record tool-driven file events."""
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.0, "max_tokens": 1000},
        "tool": "openhands",
    }
    return AgentSession.create(agent_id=uuid4(), role=AgentRole.WORKER, config=config)


class TestSourceFileObservedEvent:
    """The viewed-content capture event."""

    def test_record_view_emits_source_file_observed_with_content_and_hash(self) -> None:
        # Given: a worker and the content a view tool returned
        worker = _worker()
        path = "src/vuln.c"
        content = "int main(void) { return 0; }\n"
        observer_id = worker.agent_id

        # When: the adapter records the viewed content
        worker.record_source_file_observed(path=path, content=content, observed_by=observer_id)

        # Then: a SourceFileObserved event carries the verbatim bytes + sha256
        observed = [e for e in worker.events if isinstance(e, SourceFileObserved)]
        assert len(observed) == 1
        event = observed[0]
        assert event.aggregate_id == worker.agent_id
        assert event.path == path
        assert event.content == content
        assert event.content_sha256 == hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert event.observed_by == str(observer_id)

    def test_observed_event_is_frozen(self) -> None:
        # Given: a constructed event
        event = SourceFileObserved(
            aggregate_id=uuid4(),
            sequence_number=1,
            path="a.c",
            content="x",
            content_sha256="deadbeef",
            observed_by=str(uuid4()),
        )

        # When / Then: attribute reassignment is rejected (frozen)
        with pytest.raises(ValidationError):
            event.content = "tampered"


class TestSourceFileEditedEvent:
    """The edit-marker event that invalidates a file's recorded content."""

    def test_record_edit_emits_source_file_edited(self) -> None:
        # Given: a worker that edits a file
        worker = _worker()
        path = "src/vuln.c"
        editor_id = worker.agent_id

        # When: the adapter records the edit
        worker.record_source_file_edited(path=path, edited_by=editor_id)

        # Then: a SourceFileEdited event names the path and editor
        edited = [e for e in worker.events if isinstance(e, SourceFileEdited)]
        assert len(edited) == 1
        assert edited[0].path == path
        assert edited[0].edited_by == str(editor_id)
        assert edited[0].aggregate_id == worker.agent_id

    def test_edited_event_is_frozen(self) -> None:
        # Given: a constructed event
        event = SourceFileEdited(
            aggregate_id=uuid4(),
            sequence_number=1,
            path="a.c",
            edited_by=str(uuid4()),
        )

        # When / Then: attribute reassignment is rejected (frozen)
        with pytest.raises(ValidationError):
            event.path = "b.c"


class TestSourceFileEventsReplay:
    """Recoverability: state must round-trip through load_from_history."""

    def test_observe_and_edit_replay_identically(self) -> None:
        # Given: a worker that views then edits a file, with sequential numbering
        worker = _worker()
        worker.record_source_file_observed(
            path="src/vuln.c", content="orig\n", observed_by=worker.agent_id
        )
        worker.record_source_file_edited(path="src/vuln.c", edited_by=worker.agent_id)
        history = list(worker.events)

        # When: the aggregate is rebuilt purely from its event log
        replayed = AgentSession.load_from_history(history)

        # Then: the rebuild succeeds and sequence numbers are strictly increasing
        sequences = [e.sequence_number for e in history]
        assert sequences == sorted(set(sequences))
        assert replayed.agent_id == worker.agent_id
        # And: version advanced once per emitted event (OCC bookkeeping)
        assert replayed.version == worker.version

    def test_observed_content_recoverable_from_events_alone(self) -> None:
        # Given: two distinct files viewed by the worker
        worker = _worker()
        files = {"a.c": "alpha\n", "b.c": "beta\n"}
        for path, content in files.items():
            worker.record_source_file_observed(
                path=path, content=content, observed_by=worker.agent_id
            )
        history = list(worker.events)

        # When: reconstructing the latest observed content from the log alone
        recovered: dict[str, str] = {}
        for event in history:
            if isinstance(event, SourceFileObserved):
                recovered[event.path] = event.content

        # Then: every byte is recoverable and integrity-checks against its hash
        assert recovered == files
        for event in history:
            if isinstance(event, SourceFileObserved):
                digest = hashlib.sha256(event.content.encode("utf-8")).hexdigest()
                assert event.content_sha256 == digest
