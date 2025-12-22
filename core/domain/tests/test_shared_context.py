"""Test cases for SharedExecutionContext aggregate.

Tests the event-sourced shared context with budget tracking,
artifacts, decisions, and progress.
"""

from uuid import uuid4

from core.domain.events import (
    ArtifactStored,
    BudgetConsumed,
    BudgetExceeded,
    DecisionRecorded,
    ProgressUpdated,
    SharedContextCreated,
)
from core.domain.shared_context import SharedExecutionContext


class TestSharedContextCreation:
    """Tests for SharedExecutionContext creation."""

    def test_create_basic(self) -> None:
        """Test creating a basic shared context."""
        root_id = uuid4()
        context = SharedExecutionContext.create(root_id)

        assert context.root_id == root_id
        assert context.version == 1
        assert context.initial_budget_usd == 0.0
        assert context.consumed_budget_usd == 0.0
        assert not context.budget_exceeded

    def test_create_with_budget(self) -> None:
        """Test creating context with initial budget."""
        root_id = uuid4()
        context = SharedExecutionContext.create(
            root_id=root_id,
            initial_budget_usd=10.0,
            config={"max_workers": 5},
        )

        assert context.initial_budget_usd == 10.0
        assert context.get_remaining_budget() == 10.0

    def test_creation_emits_event(self) -> None:
        """Test that creation emits SharedContextCreated event."""
        root_id = uuid4()
        context = SharedExecutionContext.create(root_id, initial_budget_usd=5.0)

        assert len(context.events) == 1
        event = context.events[0]
        assert isinstance(event, SharedContextCreated)
        assert event.root_id == root_id
        assert event.initial_budget_usd == 5.0


class TestSharedContextArtifacts:
    """Tests for artifact storage in shared context."""

    def test_store_artifact(self) -> None:
        """Test storing an artifact."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)
        context.mark_changes_as_committed()

        context.store_artifact(
            key="output/analysis.json",
            content_type="application/json",
            stored_by=agent_id,
            content='{"result": "success"}',
        )

        artifact = context.get_artifact("output/analysis.json")
        assert artifact is not None
        assert artifact.key == "output/analysis.json"
        assert artifact.content_type == "application/json"
        assert artifact.content == '{"result": "success"}'
        assert artifact.stored_by == agent_id

    def test_store_artifact_emits_event(self) -> None:
        """Test that storing artifact emits ArtifactStored event."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)
        context.mark_changes_as_committed()

        context.store_artifact(
            key="file.txt",
            content_type="text/plain",
            stored_by=agent_id,
            content="hello",
        )

        assert len(context.events) == 1
        event = context.events[0]
        assert isinstance(event, ArtifactStored)
        assert event.key == "file.txt"

    def test_list_artifacts(self) -> None:
        """Test listing all artifact keys."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)

        context.store_artifact("a.txt", "text/plain", agent_id)
        context.store_artifact("b.txt", "text/plain", agent_id)

        keys = context.list_artifacts()
        assert "a.txt" in keys
        assert "b.txt" in keys


class TestSharedContextDecisions:
    """Tests for decision recording in shared context."""

    def test_record_decision(self) -> None:
        """Test recording a decision."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)
        context.mark_changes_as_committed()

        context.record_decision(
            decision_key="architecture.database",
            decision_value="postgresql",
            rationale="Best for our use case",
            decided_by=agent_id,
        )

        decision = context.get_decision("architecture.database")
        assert decision is not None
        assert decision.value == "postgresql"
        assert decision.rationale == "Best for our use case"
        assert decision.decided_by == agent_id

    def test_record_decision_emits_event(self) -> None:
        """Test that recording decision emits DecisionRecorded event."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)
        context.mark_changes_as_committed()

        context.record_decision(
            decision_key="tech.framework",
            decision_value="fastapi",
            rationale="Fast and modern",
            decided_by=agent_id,
        )

        assert len(context.events) == 1
        event = context.events[0]
        assert isinstance(event, DecisionRecorded)
        assert event.decision_key == "tech.framework"


class TestSharedContextProgress:
    """Tests for progress tracking in shared context."""

    def test_update_progress(self) -> None:
        """Test updating progress checkpoint."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)
        context.mark_changes_as_committed()

        context.update_progress(
            checkpoint_key="setup",
            status="completed",
            reported_by=agent_id,
            progress_pct=100.0,
            message="Setup complete",
        )

        progress = context.get_progress("setup")
        assert progress is not None
        assert progress.status == "completed"
        assert progress.progress_pct == 100.0
        assert progress.message == "Setup complete"

    def test_update_progress_emits_event(self) -> None:
        """Test that updating progress emits ProgressUpdated event."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id)
        context.mark_changes_as_committed()

        context.update_progress(
            checkpoint_key="build",
            status="in_progress",
            reported_by=agent_id,
            progress_pct=50.0,
        )

        assert len(context.events) == 1
        event = context.events[0]
        assert isinstance(event, ProgressUpdated)
        assert event.checkpoint_key == "build"
        assert event.status == "in_progress"


class TestSharedContextBudget:
    """Tests for budget tracking in shared context."""

    def test_consume_budget(self) -> None:
        """Test consuming budget."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id, initial_budget_usd=10.0)
        context.mark_changes_as_committed()

        context.consume_budget(
            agent_id=agent_id,
            amount=2.5,
            operation="llm_call",
            model="gpt-4",
            tokens={"prompt": 100, "completion": 50},
        )

        assert context.consumed_budget_usd == 2.5
        assert context.get_remaining_budget() == 7.5

    def test_consume_budget_emits_event(self) -> None:
        """Test that consuming budget emits BudgetConsumed event."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id, initial_budget_usd=10.0)
        context.mark_changes_as_committed()

        context.consume_budget(
            agent_id=agent_id,
            amount=1.0,
            operation="worker_execution",
        )

        assert len(context.events) == 1
        event = context.events[0]
        assert isinstance(event, BudgetConsumed)
        assert event.amount_usd == 1.0
        assert event.operation == "worker_execution"

    def test_budget_exceeded_triggers_event(self) -> None:
        """Test that exceeding budget triggers BudgetExceeded event."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id, initial_budget_usd=5.0)
        context.mark_changes_as_committed()

        # First consumption
        context.consume_budget(agent_id, 3.0, "llm_call")
        context.mark_changes_as_committed()

        # This should trigger exceeded
        context.consume_budget(agent_id, 3.0, "llm_call")

        # Should have 2 events: BudgetConsumed + BudgetExceeded
        assert len(context.events) == 2
        assert isinstance(context.events[0], BudgetConsumed)
        assert isinstance(context.events[1], BudgetExceeded)
        assert context.budget_exceeded

    def test_unlimited_budget(self) -> None:
        """Test that 0 initial budget means unlimited."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id, initial_budget_usd=0.0)
        context.mark_changes_as_committed()

        context.consume_budget(agent_id, 1000.0, "llm_call")

        assert context.get_remaining_budget() == -1.0  # -1 means unlimited
        assert not context.budget_exceeded

    def test_budget_summary(self) -> None:
        """Test getting budget summary."""
        root_id = uuid4()
        agent_id = uuid4()
        context = SharedExecutionContext.create(root_id, initial_budget_usd=10.0)
        context.consume_budget(agent_id, 3.0, "llm_call")

        summary = context.get_budget_summary()

        assert summary["initial_budget_usd"] == 10.0
        assert summary["consumed_budget_usd"] == 3.0
        assert summary["remaining_budget_usd"] == 7.0
        assert summary["budget_exceeded"] is False


class TestSharedContextEventReplay:
    """Tests for event sourcing replay."""

    def test_load_from_history(self) -> None:
        """Test reconstructing context from event history."""
        root_id = uuid4()
        agent_id = uuid4()

        # Create context and perform some operations
        original = SharedExecutionContext.create(root_id, initial_budget_usd=10.0)
        original.store_artifact("test.txt", "text/plain", agent_id, content="hello")
        original.record_decision("key", "value", "reason", agent_id)
        original.consume_budget(agent_id, 2.0, "llm_call")

        # Get all events
        all_events = list(original.events)

        # Reconstruct from events
        reconstructed = SharedExecutionContext.load_from_history(all_events)

        # Verify state matches
        assert reconstructed.root_id == original.root_id
        assert reconstructed.initial_budget_usd == original.initial_budget_usd
        assert reconstructed.consumed_budget_usd == original.consumed_budget_usd
        assert reconstructed.get_artifact("test.txt") is not None
        assert reconstructed.get_decision("key") is not None

    def test_mark_changes_as_committed(self) -> None:
        """Test that marking changes as committed clears events."""
        root_id = uuid4()
        context = SharedExecutionContext.create(root_id)

        assert len(context.events) == 1
        context.mark_changes_as_committed()
        assert len(context.events) == 0
