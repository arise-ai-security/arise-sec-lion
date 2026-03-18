"""Test cases for SharedStore aggregate.

Tests the event-sourced shared store with artifacts and decisions.
"""

from uuid import uuid4

from core.domain.events.events import (
    ArtifactStored,
    DecisionRecorded,
    SharedContextCreated,
)
from core.domain.shared_context import SharedStore


class TestSharedStoreCreation:
    """Tests for SharedStore creation."""

    def test_create_basic(self) -> None:
        """Test creating a basic shared store."""
        root_id = uuid4()
        store = SharedStore.create(root_id)

        assert store.root_id == root_id
        assert store.version == 1

    def test_create_with_config(self) -> None:
        """Test creating store with config."""
        root_id = uuid4()
        store = SharedStore.create(
            root_id=root_id,
            config={"max_workers": 5},
        )

        assert store.root_id == root_id
        assert store.version == 1

    def test_creation_emits_event(self) -> None:
        """Test that creation emits SharedContextCreated event."""
        root_id = uuid4()
        store = SharedStore.create(root_id)

        assert len(store.events) == 1
        event = store.events[0]
        assert isinstance(event, SharedContextCreated)
        assert event.root_id == root_id


class TestSharedStoreArtifacts:
    """Tests for artifact storage in shared store."""

    def test_store_artifact(self) -> None:
        """Test storing an artifact."""
        root_id = uuid4()
        agent_id = uuid4()
        store = SharedStore.create(root_id)
        store.mark_changes_as_committed()

        store.store_artifact(
            key="output/analysis.json",
            content_type="application/json",
            stored_by=agent_id,
            content='{"result": "success"}',
        )

        artifact = store.get_artifact("output/analysis.json")
        assert artifact is not None
        assert artifact.key == "output/analysis.json"
        assert artifact.content_type == "application/json"
        assert artifact.content == '{"result": "success"}'
        assert artifact.stored_by == agent_id

    def test_store_artifact_emits_event(self) -> None:
        """Test that storing artifact emits ArtifactStored event."""
        root_id = uuid4()
        agent_id = uuid4()
        store = SharedStore.create(root_id)
        store.mark_changes_as_committed()

        store.store_artifact(
            key="file.txt",
            content_type="text/plain",
            stored_by=agent_id,
            content="hello",
        )

        assert len(store.events) == 1
        event = store.events[0]
        assert isinstance(event, ArtifactStored)
        assert event.key == "file.txt"

    def test_list_artifacts(self) -> None:
        """Test listing all artifact keys."""
        root_id = uuid4()
        agent_id = uuid4()
        store = SharedStore.create(root_id)

        store.store_artifact("a.txt", "text/plain", agent_id)
        store.store_artifact("b.txt", "text/plain", agent_id)

        keys = store.list_artifacts()
        assert "a.txt" in keys
        assert "b.txt" in keys


class TestSharedStoreDecisions:
    """Tests for decision recording in shared store."""

    def test_record_decision(self) -> None:
        """Test recording a decision."""
        root_id = uuid4()
        agent_id = uuid4()
        store = SharedStore.create(root_id)
        store.mark_changes_as_committed()

        store.record_decision(
            decision_key="architecture.database",
            decision_value="postgresql",
            rationale="Best for our use case",
            decided_by=agent_id,
        )

        decision = store.get_decision("architecture.database")
        assert decision is not None
        assert decision.value == "postgresql"
        assert decision.rationale == "Best for our use case"
        assert decision.decided_by == agent_id

    def test_record_decision_emits_event(self) -> None:
        """Test that recording decision emits DecisionRecorded event."""
        root_id = uuid4()
        agent_id = uuid4()
        store = SharedStore.create(root_id)
        store.mark_changes_as_committed()

        store.record_decision(
            decision_key="tech.framework",
            decision_value="fastapi",
            rationale="Fast and modern",
            decided_by=agent_id,
        )

        assert len(store.events) == 1
        event = store.events[0]
        assert isinstance(event, DecisionRecorded)
        assert event.decision_key == "tech.framework"


class TestSharedStoreEventReplay:
    """Tests for event sourcing replay."""

    def test_load_from_history(self) -> None:
        """Test reconstructing store from event history."""
        root_id = uuid4()
        agent_id = uuid4()

        # Create store and perform some operations
        original = SharedStore.create(root_id)
        original.store_artifact("test.txt", "text/plain", agent_id, content="hello")
        original.record_decision("key", "value", "reason", agent_id)

        # Get all events
        all_events = list(original.events)

        # Reconstruct from events
        reconstructed = SharedStore.load_from_history(all_events)

        # Verify state matches
        assert reconstructed.root_id == original.root_id
        assert reconstructed.get_artifact("test.txt") is not None
        assert reconstructed.get_decision("key") is not None

    def test_mark_changes_as_committed(self) -> None:
        """Test that marking changes as committed clears events."""
        root_id = uuid4()
        store = SharedStore.create(root_id)

        assert len(store.events) == 1
        store.mark_changes_as_committed()
        assert len(store.events) == 0
