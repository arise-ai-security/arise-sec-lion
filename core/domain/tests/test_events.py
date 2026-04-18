"""Tests for domain event immutability.

These tests verify that frozen events with dict fields are truly immutable -
external references to dicts passed during construction cannot mutate the event.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.domain.events.events import (
    AgentCreated,
    ArtifactStored,
    ChildCompleted,
    ChildSpawned,
    SharedContextCreated,
    TaskAssigned,
    ThoughtCaptured,
)
from core.domain.values.subtask import Subtask


class TestEventImmutability:
    """Tests that frozen events cannot be mutated."""

    def test_domain_event_metadata_is_deep_copied(self):
        """External dict mutation should not affect event metadata."""
        # Given: A dict that will be passed as metadata
        external_dict = {"key": "value", "nested": {"inner": 1}}
        agent_id = uuid4()

        # When: Creating an event with the dict
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role="BOSS",
            metadata=external_dict,
        )

        # Then: Mutating the external dict should not affect the event
        external_dict["key"] = "mutated"
        external_dict["nested"]["inner"] = 999

        assert event.metadata["key"] == "value"
        assert event.metadata["nested"]["inner"] == 1

    def test_agent_created_config_is_deep_copied(self):
        """External config dict mutation should not affect event."""
        # Given
        config = {"model": "gpt-4", "params": {"temperature": 0.7}}
        agent_id = uuid4()

        # When
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role="WORKER",
            config=config,
        )

        # Then: External mutation does not affect event
        config["model"] = "gpt-3.5"
        config["params"]["temperature"] = 1.0

        assert event.config["model"] == "gpt-4"
        assert event.config["params"]["temperature"] == 0.7

    def test_task_assigned_constraints_is_deep_copied(self):
        """External constraints dict mutation should not affect event."""
        # Given
        constraints = {"max_tokens": 1000, "rules": ["no_sql", "use_orm"]}
        agent_id = uuid4()

        # When
        event = TaskAssigned(
            aggregate_id=agent_id,
            sequence_number=1,
            task_description="Build API",
            constraints=constraints,
        )

        # Then
        constraints["max_tokens"] = 500
        constraints["rules"].append("extra_rule")

        assert event.constraints["max_tokens"] == 1000
        assert event.constraints["rules"] == ["no_sql", "use_orm"]

    def test_child_spawned_dicts_are_deep_copied(self):
        """Both child_config and briefing should be deep copied."""
        # Given
        child_config = {"tool": "claude", "settings": {"verbose": True}}
        briefing = {"parent_task": "main", "parent_role": "boss"}
        parent_id = uuid4()
        child_id = uuid4()

        # When
        event = ChildSpawned(
            aggregate_id=parent_id,
            sequence_number=1,
            child_id=child_id,
            child_role="PENDING",
            subtask=Subtask(description="Sub task", config={}),
            child_config=child_config,
            briefing=briefing,
        )

        # Then
        child_config["tool"] = "openhands"
        child_config["settings"]["verbose"] = False
        briefing["parent_task"] = "changed"

        assert event.child_config["tool"] == "claude"
        assert event.child_config["settings"]["verbose"] is True
        assert event.briefing["parent_task"] == "main"

    def test_child_completed_report_is_deep_copied(self):
        """report dict should be deep copied."""
        # Given
        report = {"direction": "up", "result": "success", "artifacts": ["a.py", "b.py"]}
        parent_id = uuid4()
        child_id = uuid4()

        # When
        event = ChildCompleted(
            aggregate_id=parent_id,
            sequence_number=1,
            child_id=child_id,
            result="Done",
            report=report,
        )

        # Then
        report["result"] = "failed"
        report["artifacts"].append("c.py")

        assert event.report["result"] == "success"
        assert event.report["artifacts"] == ["a.py", "b.py"]

    def test_shared_context_created_config_is_deep_copied(self):
        """SharedContextCreated config should be deep copied."""
        # Given
        config = {"budget": 100, "options": {"parallel": True}}
        root_id = uuid4()

        # When
        event = SharedContextCreated(
            aggregate_id=root_id,
            sequence_number=1,
            root_id=root_id,
            config=config,
        )

        # Then
        config["budget"] = 0
        config["options"]["parallel"] = False

        assert event.config["budget"] == 100
        assert event.config["options"]["parallel"] is True

    def test_artifact_stored_metadata_is_deep_copied(self):
        """ArtifactStored metadata should be deep copied."""
        # Given
        metadata = {"size": 1024, "tags": ["code", "python"]}
        agent_id = uuid4()

        # When
        event = ArtifactStored(
            aggregate_id=agent_id,
            sequence_number=1,
            key="output.py",
            content_type="text/python",
            stored_by=agent_id,
            metadata=metadata,
        )

        # Then
        metadata["size"] = 0
        metadata["tags"].append("test")

        assert event.metadata["size"] == 1024
        assert event.metadata["tags"] == ["code", "python"]

class TestFrozenEventAttributeAssignment:
    """Tests that frozen events reject attribute assignment."""

    def test_cannot_reassign_event_attribute(self):
        """Frozen events should reject direct attribute assignment."""
        event = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=1,
            role="BOSS",
        )

        with pytest.raises(ValidationError):
            event.role = "WORKER"

    def test_cannot_reassign_event_dict_field(self):
        """Frozen events should reject dict field reassignment."""
        event = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=1,
            role="BOSS",
            config={"model": "gpt-4"},
        )

        with pytest.raises(ValidationError):
            event.config = {"model": "gpt-3.5"}


class TestEventDictMutationAttempts:
    """Tests that attempting to mutate event dicts in-place is isolated.

    Note: Python dicts are mutable, so event.config["key"] = "new" will
    actually succeed. The deep copy ensures this mutation doesn't affect
    the original source dict, but the event's internal dict is technically
    mutable. For complete immutability, consider using MappingProxyType or
    custom frozen dict wrappers.
    """

    def test_internal_dict_mutation_does_not_affect_source(self):
        """Verify source isolation: mutating event dict doesn't affect original."""
        # Given
        source_config = {"model": "gpt-4"}
        event = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=1,
            role="BOSS",
            config=source_config,
        )

        # When: Mutate event's internal dict (this is allowed by Python)
        event.config["model"] = "changed"

        # Then: Source dict is unaffected (deep copy worked)
        assert source_config["model"] == "gpt-4"

    def test_default_factory_returns_fresh_dicts(self):
        """Events with default dict factories get independent dicts."""
        event1 = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=1,
            role="BOSS",
        )
        event2 = AgentCreated(
            aggregate_id=uuid4(),
            sequence_number=2,
            role="WORKER",
        )

        # Mutate one event's config
        event1.config["test"] = True

        # Other event should be unaffected
        assert "test" not in event2.config


class TestThoughtCapturedToolCallFields:
    """Tests for the call_id/duration_ms fields added for per-tool-call timing."""

    def test_thought_captured_accepts_call_id_and_duration_ms(self) -> None:
        """ThoughtCaptured preserves the new tool-call correlation fields."""
        # Given: a ThoughtCaptured with new optional fields populated
        event = ThoughtCaptured(
            aggregate_id=uuid4(),
            sequence_number=5,
            content="tool result",
            call_id="tu_abc",
            duration_ms=42,
        )

        # Then: fields round-trip through construction and serialization
        assert event.call_id == "tu_abc"
        assert event.duration_ms == 42
        dumped = event.model_dump()
        assert dumped["call_id"] == "tu_abc"
        assert dumped["duration_ms"] == 42

    def test_thought_captured_defaults_for_new_fields_keep_backward_compat(self) -> None:
        """call_id and duration_ms default to None when omitted."""
        # When: constructed without the new fields
        event = ThoughtCaptured(
            aggregate_id=uuid4(),
            sequence_number=1,
            content="x",
        )

        # Then: defaults are None (backward-compatible with historical events)
        assert event.call_id is None
        assert event.duration_ms is None
