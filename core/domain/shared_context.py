"""Shared Execution Context - event-sourced aggregate for shared state.

One instance per execution hierarchy (keyed by root_id).
Provides: artifacts, decisions, progress, config overrides, and budget tracking.

All changes are event-sourced for OCC and auditability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import singledispatchmethod
from typing import Any
from uuid import UUID, uuid5

from core.domain.events import (
    ArtifactStored,
    BudgetConsumed,
    BudgetExceeded,
    ConfigOverrideSet,
    DecisionRecorded,
    DomainEvent,
    ProgressUpdated,
    SharedContextCreated,
)

# Fixed namespace UUID for deriving SharedExecutionContext aggregate IDs.
# This ensures SharedExecutionContext has a unique aggregate_id derived from root_id,
# avoiding collision with AgentSession which also uses root_id as its aggregate_id.
SHARED_CONTEXT_NAMESPACE = UUID("b8f9e3a1-7c2d-4f5e-9a1b-3c4d5e6f7a8b")


def shared_context_aggregate_id(root_id: UUID) -> UUID:
    """Derive a unique aggregate_id for SharedExecutionContext from root_id.

    Uses UUID5 (SHA-1 based) to create a deterministic but unique ID.
    This allows lookup by root_id while avoiding PK collision with AgentSession.

    Args:
        root_id: The root agent's session ID.

    Returns:
        A derived UUID for use as the SharedExecutionContext's aggregate_id.
    """
    return uuid5(SHARED_CONTEXT_NAMESPACE, str(root_id))


@dataclass
class Artifact:
    """Stored artifact metadata."""

    key: str
    content_type: str
    content: str | None
    content_hash: str | None
    stored_by: UUID
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Recorded decision."""

    key: str
    value: str
    rationale: str
    decided_by: UUID


@dataclass
class ProgressCheckpoint:
    """Progress tracking checkpoint."""

    key: str
    status: str
    progress_pct: float
    message: str
    reported_by: UUID


@dataclass
class ConfigOverride:
    """Configuration override."""

    key: str
    value: str
    set_by: UUID
    scope: str


class SharedExecutionContext:
    """Event-sourced aggregate for shared state across agent hierarchy.

    One instance per execution hierarchy (keyed by root_id).
    Provides centralized storage for:
    - Artifacts: Shared outputs between agents
    - Decisions: Architectural/design choices
    - Progress: Checkpoint tracking
    - Config overrides: Runtime configuration
    - Budget: Cost tracking with enforcement

    All state changes are event-sourced for consistency and auditability.
    """

    def __init__(self, root_id: UUID) -> None:
        """Internal. Use create() or load_from_history() instead."""
        self._root_id = root_id
        self._version = 0
        self._changes: list[DomainEvent] = []
        self._sequence = 0

        # State
        self._artifacts: dict[str, Artifact] = {}
        self._decisions: dict[str, Decision] = {}
        self._progress: dict[str, ProgressCheckpoint] = {}
        self._config_overrides: dict[str, ConfigOverride] = {}

        # Budget state
        self._initial_budget_usd: float = 0.0
        self._consumed_budget_usd: float = 0.0
        self._budget_exceeded: bool = False

        # General config
        self._config: dict[str, Any] = {}

    @property
    def root_id(self) -> UUID:
        """Root agent ID that this context belongs to."""
        return self._root_id

    @property
    def aggregate_id(self) -> UUID:
        """Derived aggregate ID for event store (distinct from root_id)."""
        return shared_context_aggregate_id(self._root_id)

    @property
    def version(self) -> int:
        """Current version (for OCC)."""
        return self._version

    @property
    def events(self) -> list[DomainEvent]:
        """Uncommitted events pending persistence."""
        return self._changes

    @property
    def initial_budget_usd(self) -> float:
        """Initial budget limit."""
        return self._initial_budget_usd

    @property
    def consumed_budget_usd(self) -> float:
        """Total consumed budget."""
        return self._consumed_budget_usd

    @property
    def budget_exceeded(self) -> bool:
        """Whether budget has been exceeded."""
        return self._budget_exceeded

    # -------------------------------------------------------------------------
    # Factory Methods
    # -------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root_id: UUID,
        initial_budget_usd: float = 0.0,
        config: dict[str, Any] | None = None,
    ) -> SharedExecutionContext:
        """Create new shared context for an execution run.

        Args:
            root_id: Root agent ID (becomes aggregate ID)
            initial_budget_usd: Budget limit for the run (0 = unlimited)
            config: Optional configuration dict

        Returns:
            New SharedExecutionContext with creation event
        """
        instance = cls(root_id)
        event = SharedContextCreated(
            aggregate_id=instance.aggregate_id,
            sequence_number=instance._next_sequence(),
            root_id=root_id,
            initial_budget_usd=initial_budget_usd,
            config=config or {},
        )
        instance._apply(event)
        instance._changes.append(event)
        return instance

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> SharedExecutionContext:
        """Reconstruct SharedExecutionContext by replaying events.

        Args:
            events: Event history to replay

        Returns:
            Reconstructed SharedExecutionContext

        Raises:
            AssertionError: If events list is empty or first event is not SharedContextCreated
        """
        assert events, "Cannot load from empty event history"
        first_event = events[0]
        assert isinstance(first_event, SharedContextCreated), (
            f"First event must be SharedContextCreated, got {type(first_event).__name__}"
        )

        instance = cls(first_event.root_id)
        for event in events:
            instance._apply(event)
            instance._sequence = max(instance._sequence, event.sequence_number)
        return instance

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        self._changes.clear()

    # -------------------------------------------------------------------------
    # Commands - Artifacts
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
        """Store an artifact in shared context.

        Args:
            key: Artifact key (e.g., "outputs/analysis.json")
            content_type: MIME type
            stored_by: Agent storing the artifact
            content: Small content (inline)
            content_hash: SHA256 hash for large content
            **metadata: Additional metadata
        """
        event = ArtifactStored(
            aggregate_id=self.aggregate_id,
            sequence_number=self._next_sequence(),
            key=key,
            content_type=content_type,
            content=content,
            content_hash=content_hash,
            stored_by=stored_by,
            metadata=dict(metadata),
        )
        self._apply(event)
        self._changes.append(event)

    def get_artifact(self, key: str) -> Artifact | None:
        """Get artifact by key."""
        return self._artifacts.get(key)

    def list_artifacts(self) -> list[str]:
        """List all artifact keys."""
        return list(self._artifacts.keys())

    # -------------------------------------------------------------------------
    # Commands - Decisions
    # -------------------------------------------------------------------------

    def record_decision(
        self,
        decision_key: str,
        decision_value: str,
        rationale: str,
        decided_by: UUID,
    ) -> None:
        """Record a key decision in shared context.

        Args:
            decision_key: Decision identifier (e.g., "architecture.database")
            decision_value: The decision value (JSON-serialized if complex)
            rationale: Why this decision was made
            decided_by: Agent making the decision
        """
        event = DecisionRecorded(
            aggregate_id=self.aggregate_id,
            sequence_number=self._next_sequence(),
            decision_key=decision_key,
            decision_value=decision_value,
            rationale=rationale,
            decided_by=decided_by,
        )
        self._apply(event)
        self._changes.append(event)

    def get_decision(self, key: str) -> Decision | None:
        """Get decision by key."""
        return self._decisions.get(key)

    def list_decisions(self) -> list[str]:
        """List all decision keys."""
        return list(self._decisions.keys())

    # -------------------------------------------------------------------------
    # Commands - Progress
    # -------------------------------------------------------------------------

    def update_progress(
        self,
        checkpoint_key: str,
        status: str,
        reported_by: UUID,
        progress_pct: float = 0.0,
        message: str = "",
    ) -> None:
        """Update a progress checkpoint.

        Args:
            checkpoint_key: Checkpoint identifier
            status: Status (started, in_progress, completed, blocked)
            reported_by: Agent reporting progress
            progress_pct: Progress percentage (0-100)
            message: Optional message
        """
        event = ProgressUpdated(
            aggregate_id=self.aggregate_id,
            sequence_number=self._next_sequence(),
            checkpoint_key=checkpoint_key,
            status=status,
            progress_pct=progress_pct,
            message=message,
            reported_by=reported_by,
        )
        self._apply(event)
        self._changes.append(event)

    def get_progress(self, checkpoint_key: str) -> ProgressCheckpoint | None:
        """Get progress checkpoint by key."""
        return self._progress.get(checkpoint_key)

    # -------------------------------------------------------------------------
    # Commands - Config Overrides
    # -------------------------------------------------------------------------

    def set_config_override(
        self,
        config_key: str,
        config_value: str,
        set_by: UUID,
        scope: str = "global",
    ) -> None:
        """Set a configuration override.

        Args:
            config_key: Config key to override
            config_value: New value (JSON-serialized if complex)
            set_by: Agent setting the override
            scope: "global" or "subtree:{agent_id}"
        """
        event = ConfigOverrideSet(
            aggregate_id=self.aggregate_id,
            sequence_number=self._next_sequence(),
            config_key=config_key,
            config_value=config_value,
            set_by=set_by,
            scope=scope,
        )
        self._apply(event)
        self._changes.append(event)

    def get_config_override(self, key: str) -> ConfigOverride | None:
        """Get config override by key."""
        return self._config_overrides.get(key)

    # -------------------------------------------------------------------------
    # Commands - Budget
    # -------------------------------------------------------------------------

    def consume_budget(
        self,
        agent_id: UUID,
        amount: float,
        operation: str,
        model: str | None = None,
        tokens: dict[str, int] | None = None,
    ) -> None:
        """Record budget consumption.

        Emits BudgetConsumed event. If consumption exceeds limit,
        also emits BudgetExceeded event.

        Args:
            agent_id: Agent consuming budget
            amount: Cost in USD
            operation: Operation type ("llm_call", "worker_execution")
            model: Model used (if applicable)
            tokens: Token counts {"prompt": N, "completion": M}
        """
        event = BudgetConsumed(
            aggregate_id=self.aggregate_id,
            sequence_number=self._next_sequence(),
            consumed_by=agent_id,
            amount_usd=amount,
            operation=operation,
            model=model,
            tokens=tokens or {},
        )
        self._apply(event)
        self._changes.append(event)

        # Check for budget exceeded
        if self._initial_budget_usd > 0 and not self._budget_exceeded:
            if self._consumed_budget_usd >= self._initial_budget_usd:
                exceeded_event = BudgetExceeded(
                    aggregate_id=self.aggregate_id,
                    sequence_number=self._next_sequence(),
                    limit_usd=self._initial_budget_usd,
                    consumed_usd=self._consumed_budget_usd,
                    triggered_by=agent_id,
                )
                self._apply(exceeded_event)
                self._changes.append(exceeded_event)

    def get_remaining_budget(self) -> float:
        """Get remaining budget in USD.

        Returns:
            Remaining budget, or -1 if unlimited (initial_budget = 0)
        """
        if self._initial_budget_usd <= 0:
            return -1.0  # Unlimited
        return max(0.0, self._initial_budget_usd - self._consumed_budget_usd)

    def is_budget_exceeded(self) -> bool:
        """Check if budget limit has been exceeded."""
        return self._budget_exceeded

    def get_budget_summary(self) -> dict[str, Any]:
        """Get budget summary."""
        return {
            "initial_budget_usd": self._initial_budget_usd,
            "consumed_budget_usd": self._consumed_budget_usd,
            "remaining_budget_usd": self.get_remaining_budget(),
            "budget_exceeded": self._budget_exceeded,
        }

    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------

    def _next_sequence(self) -> int:
        """Get next sequence number."""
        self._sequence += 1
        return self._sequence

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply event to update state."""
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: SharedContextCreated) -> None:
        self._initial_budget_usd = event.initial_budget_usd
        self._config = event.config
        self._version += 1

    @_apply.register
    def _(self, event: ArtifactStored) -> None:
        self._artifacts[event.key] = Artifact(
            key=event.key,
            content_type=event.content_type,
            content=event.content,
            content_hash=event.content_hash,
            stored_by=event.stored_by,
            metadata=event.metadata,
        )
        self._version += 1

    @_apply.register
    def _(self, event: DecisionRecorded) -> None:
        self._decisions[event.decision_key] = Decision(
            key=event.decision_key,
            value=event.decision_value,
            rationale=event.rationale,
            decided_by=event.decided_by,
        )
        self._version += 1

    @_apply.register
    def _(self, event: ProgressUpdated) -> None:
        self._progress[event.checkpoint_key] = ProgressCheckpoint(
            key=event.checkpoint_key,
            status=event.status,
            progress_pct=event.progress_pct,
            message=event.message,
            reported_by=event.reported_by,
        )
        self._version += 1

    @_apply.register
    def _(self, event: ConfigOverrideSet) -> None:
        self._config_overrides[event.config_key] = ConfigOverride(
            key=event.config_key,
            value=event.config_value,
            set_by=event.set_by,
            scope=event.scope,
        )
        self._version += 1

    @_apply.register
    def _(self, event: BudgetConsumed) -> None:
        self._consumed_budget_usd += event.amount_usd
        self._version += 1

    @_apply.register
    def _(self, event: BudgetExceeded) -> None:
        self._budget_exceeded = True
        self._version += 1
