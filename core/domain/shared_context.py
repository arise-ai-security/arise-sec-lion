"""Shared Store - event-sourced aggregates for shared state.

One SharedStore instance per execution hierarchy (keyed by root_id).
Composed of focused aggregates following Single Responsibility Principle:
- ArtifactStore: Shared outputs between agents
- DecisionLog: Architectural/design choices

All changes are event-sourced for OCC and auditability.
"""


from dataclasses import dataclass, field
from functools import singledispatchmethod
from typing import Any
from uuid import UUID, uuid5

from core.domain.events.events import (
    ArtifactStored,
    DecisionRecorded,
    DomainEvent,
    SharedContextCreated,
)
from core.domain.exceptions import InvalidEventHistoryError


# Fixed namespace UUID for deriving SharedStore aggregate IDs.
# This ensures SharedStore has a unique aggregate_id derived from root_id,
# avoiding collision with AgentSession which also uses root_id as its aggregate_id.
SHARED_CONTEXT_NAMESPACE = UUID("b8f9e3a1-7c2d-4f5e-9a1b-3c4d5e6f7a8b")


def shared_context_aggregate_id(root_id: UUID) -> UUID:
    """Derive a unique aggregate_id for SharedStore from root_id.

    Uses UUID5 (SHA-1 based) to create a deterministic but unique ID.
    This allows lookup by root_id while avoiding PK collision with AgentSession.

    Args:
        root_id: The root agent's session ID.

    Returns:
        A derived UUID for use as the SharedStore's aggregate_id.
    """
    return uuid5(SHARED_CONTEXT_NAMESPACE, str(root_id))


# =============================================================================
# Value Objects
# =============================================================================


@dataclass(frozen=True)
class Artifact:
    """Stored artifact metadata."""

    key: str
    content_type: str
    content: str | None
    content_hash: str | None
    stored_by: UUID
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """Recorded decision."""

    key: str
    value: str
    rationale: str
    decided_by: UUID


class EventSourcedAggregateBase:
    """Base class providing event-sourcing boilerplate for aggregates.

    Eliminates duplication of version tracking, change collection, sequence
    management, and event application across all event-sourced aggregates.

    Subclasses must:
    1. Call super().__init__(aggregate_id) in their __init__
    2. Override _initialize_state() to set up domain-specific state
    3. Register event handlers using @_apply.register decorator
    """

    def __init__(self, aggregate_id: UUID) -> None:
        self._aggregate_id = aggregate_id
        self._version = 0
        self._changes: list[DomainEvent] = []
        self._sequence = 0
        self._initialize_state()

    def _initialize_state(self) -> None:
        """Override to initialize domain-specific state.

        Called at end of __init__. Subclasses should set up their
        domain-specific collections and fields here.
        """
        return

    @property
    def aggregate_id(self) -> UUID:
        """Aggregate identifier."""
        return self._aggregate_id

    @property
    def version(self) -> int:
        """Current version for optimistic concurrency control."""
        return self._version

    @property
    def events(self) -> list[DomainEvent]:
        """Uncommitted events pending persistence."""
        return self._changes

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        self._changes.clear()

    def _next_sequence(self) -> int:
        """Get next sequence number for event ordering."""
        self._sequence += 1
        return self._sequence

    def set_sequence(self, seq: int) -> None:
        """Set sequence number (used during replay)."""
        self._sequence = max(self._sequence, seq)

    def _increment_version(self) -> None:
        """Increment version after applying an event."""
        self._version += 1

    def _emit(self, event: DomainEvent) -> DomainEvent:
        """Apply event and record as uncommitted change.

        Args:
            event: The domain event to emit

        Returns:
            The same event (for chaining or returning to caller)
        """
        self._apply(event)
        self._changes.append(event)
        return event

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply event to update aggregate state.

        Subclasses register handlers via @_apply.register decorator.
        """
        raise TypeError(f"No handler for {type(event).__name__}")

    def apply_event(self, event: DomainEvent) -> None:
        """Apply event during replay (public for facade use)."""
        self._apply(event)


# =============================================================================
# Shared State Projections
# =============================================================================


class ArtifactStore:
    """Projection for stored artifacts."""

    def __init__(self) -> None:
        self._artifacts: dict[str, Artifact] = {}

    @staticmethod
    def create_store_event(
        *,
        aggregate_id: UUID,
        sequence_number: int,
        key: str,
        content_type: str,
        stored_by: UUID,
        content: str | None = None,
        content_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactStored:
        """Construct an ArtifactStored event for the owning aggregate."""
        return ArtifactStored(
            aggregate_id=aggregate_id,
            sequence_number=sequence_number,
            key=key,
            content_type=content_type,
            content=content,
            content_hash=content_hash,
            stored_by=stored_by,
            metadata=metadata or {},
        )

    def apply_event(
        self,
        event: ArtifactStored,
    ) -> None:
        """Apply an ArtifactStored event to the projection state."""
        self._artifacts[event.key] = Artifact(
            key=event.key,
            content_type=event.content_type,
            content=event.content,
            content_hash=event.content_hash,
            stored_by=event.stored_by,
            metadata=event.metadata,
        )

    def get_artifact(self, key: str) -> Artifact | None:
        """Get artifact by key."""
        return self._artifacts.get(key)

    def list_artifacts(self) -> list[str]:
        """List all artifact keys."""
        return list(self._artifacts.keys())


class DecisionLog:
    """Projection for shared decisions."""

    def __init__(self) -> None:
        self._decisions: dict[str, Decision] = {}

    @staticmethod
    def create_record_event(
        *,
        aggregate_id: UUID,
        sequence_number: int,
        decision_key: str,
        decision_value: str,
        rationale: str,
        decided_by: UUID,
    ) -> DecisionRecorded:
        """Construct a DecisionRecorded event for the owning aggregate."""
        return DecisionRecorded(
            aggregate_id=aggregate_id,
            sequence_number=sequence_number,
            decision_key=decision_key,
            decision_value=decision_value,
            rationale=rationale,
            decided_by=decided_by,
        )

    def apply_event(self, event: DecisionRecorded) -> None:
        """Apply a DecisionRecorded event to the projection state."""
        self._decisions[event.decision_key] = Decision(
            key=event.decision_key,
            value=event.decision_value,
            rationale=event.rationale,
            decided_by=event.decided_by,
        )

    def get_decision(self, key: str) -> Decision | None:
        """Get decision by key."""
        return self._decisions.get(key)

    def list_decisions(self) -> list[str]:
        """List all decision keys."""
        return list(self._decisions.keys())


class SharedStore(EventSourcedAggregateBase):
    """Facade composing event-sourced aggregates for shared state.

    One instance per execution hierarchy (keyed by root_id).
    Composes:
    - ArtifactStore: Shared outputs between agents
    - DecisionLog: Architectural/design choices

    All state changes are event-sourced for consistency and auditability.
    """

    _root_id: UUID
    _artifact_store: ArtifactStore
    _decision_log: DecisionLog

    def __init__(self, root_id: UUID) -> None:
        """Internal. Use create() or load_from_history() instead."""
        self._root_id = root_id
        # Derive aggregate_id before calling super().__init__
        aggregate_id = shared_context_aggregate_id(root_id)
        super().__init__(aggregate_id)

    def _initialize_state(self) -> None:
        self._artifact_store = ArtifactStore()
        self._decision_log = DecisionLog()

    @property
    def root_id(self) -> UUID:
        """Root agent ID that this context belongs to."""
        return self._root_id

    # Note: aggregate_id, version, events inherited from EventSourcedAggregateBase

    # -------------------------------------------------------------------------
    # Factory Methods
    # -------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root_id: UUID,
        config: dict[str, Any] | None = None,
    ) -> "SharedStore":
        """Create new shared store for an execution run.

        Args:
            root_id: Root agent ID (becomes aggregate ID)
            config: Optional configuration dict

        Returns:
            New SharedStore with creation event
        """
        instance = cls(root_id)
        instance._emit(
            SharedContextCreated(
                aggregate_id=instance.aggregate_id,
                sequence_number=instance._next_sequence(),
                root_id=root_id,
                config=config or {},
            )
        )
        return instance

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "SharedStore":
        """Reconstruct SharedStore by replaying events.

        Args:
            events: Event history to replay

        Returns:
            Reconstructed SharedStore

        Raises:
            InvalidEventHistoryError: If the event history is malformed.
        """
        if not events:
            raise InvalidEventHistoryError("Cannot load from empty event history")
        first_event = events[0]
        if not isinstance(first_event, SharedContextCreated):
            raise InvalidEventHistoryError(
                "First event must be SharedContextCreated, "
                f"got {type(first_event).__name__}"
            )

        instance = cls(first_event.root_id)
        for event in events:
            instance._apply(event)
            instance.set_sequence(event.sequence_number)
        return instance

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        super().mark_changes_as_committed()

    # -------------------------------------------------------------------------
    # Delegated Commands - Artifacts
    # -------------------------------------------------------------------------

    def store_artifact(
        self,
        key: str,
        content_type: str,
        stored_by: UUID,
        content: str | None = None,
        content_hash: str | None = None,
        **metadata: Any,
    ) -> None:
        """Store an artifact in shared context (delegated to ArtifactStore)."""
        event = self._artifact_store.create_store_event(
            aggregate_id=self._aggregate_id,
            sequence_number=self._next_sequence(),
            key=key,
            content_type=content_type,
            content=content,
            content_hash=content_hash,
            stored_by=stored_by,
            metadata=dict(metadata),
        )
        self._emit(event)

    def get_artifact(self, key: str) -> Artifact | None:
        """Get artifact by key (delegated to ArtifactStore)."""
        return self._artifact_store.get_artifact(key)

    def list_artifacts(self) -> list[str]:
        """List all artifact keys (delegated to ArtifactStore)."""
        return self._artifact_store.list_artifacts()

    # -------------------------------------------------------------------------
    # Delegated Commands - Decisions
    # -------------------------------------------------------------------------

    def record_decision(
        self,
        decision_key: str,
        decision_value: str,
        rationale: str,
        decided_by: UUID,
    ) -> None:
        """Record a key decision (delegated to DecisionLog)."""
        event = self._decision_log.create_record_event(
            aggregate_id=self._aggregate_id,
            sequence_number=self._next_sequence(),
            decision_key=decision_key,
            decision_value=decision_value,
            rationale=rationale,
            decided_by=decided_by,
        )
        self._emit(event)

    def get_decision(self, key: str) -> Decision | None:
        """Get decision by key (delegated to DecisionLog)."""
        return self._decision_log.get_decision(key)

    def list_decisions(self) -> list[str]:
        """List all decision keys (delegated to DecisionLog)."""
        return self._decision_log.list_decisions()

    # -------------------------------------------------------------------------
    # Event Handlers
    # Note: _next_sequence() inherited from EventSourcedAggregateBase
    # -------------------------------------------------------------------------

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply event to update state."""
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: SharedContextCreated) -> None:
        self._increment_version()

    @_apply.register
    def _(self, event: ArtifactStored) -> None:
        self._artifact_store.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: DecisionRecorded) -> None:
        self._decision_log.apply_event(event)
        self._increment_version()
