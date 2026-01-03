"""Shared Execution Context - event-sourced aggregates for shared state.

This module implements the "Global Context Share" mechanism for the agent
hierarchy. Context Share refers to the channels by which context data
flows between agents.

## Context Share Mechanisms

There are two primary context share channels:

1. **Global Context Share (SharedExecutionContext)**:
   - Accessible by ALL agents in an execution hierarchy
   - Stored in a centralized, event-sourced aggregate
   - Used for: decisions, artifacts, source context, coworker knowledge
   - Flow: Any agent → SharedExecutionContext → Any agent

2. **Local Context Share (SpawnPayload)**:
   - Passed directly from parent to child at spawn time
   - Stored in the child's spawn_payload field
   - Used for: thinker justification, budget allocation, subtask context
   - Flow: Parent → spawn_payload → Child only

## Aggregates in SharedExecutionContext

One SharedExecutionContext instance per execution hierarchy (keyed by root_id).
Composed of focused aggregates following Single Responsibility Principle:

- ArtifactStore: Shared outputs between agents
- DecisionLog: Architectural/design choices
- ProgressTracker: Checkpoint tracking
- BudgetAccount: Cost tracking with enforcement
- KnowledgeStore: Published coworker knowledge (Design Choice 5)
- SourceContextStore: Extracted boss context (Design Choice 6 & 7)

All changes are event-sourced for OCC and auditability.
"""



from dataclasses import dataclass, field
from functools import singledispatchmethod
from typing import Any, Protocol
from uuid import UUID, uuid5

from core.domain.events.events import (
    ArtifactStored,
    BudgetConsumed,
    BudgetExceeded,
    ConfigOverrideSet,
    DecisionRecorded,
    DomainEvent,
    KnowledgePublished,
    ProgressUpdated,
    SharedContextCreated,
    SourceContextExtracted,
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


@dataclass(frozen=True)
class ProgressCheckpoint:
    """Progress tracking checkpoint."""

    key: str
    status: str
    progress_pct: float
    message: str
    reported_by: UUID


@dataclass(frozen=True)
class ConfigOverride:
    """Configuration override."""

    key: str
    value: str
    set_by: UUID
    scope: str


@dataclass(frozen=True)
class PublishedKnowledge:
    """Published worker knowledge for cross-worker learning (Design Choice 5)."""

    key: str
    objective: str
    relevance: str
    key_findings: tuple[str, ...]
    deliverables: tuple[str, ...]
    source_worker_id: UUID
    published_by: UUID


@dataclass(frozen=True)
class StoredSourceContext:
    """Stored source context extracted from boss prompt (Design Choice 6 & 7).

    Contains structured key information from user input plus inferred CWE patterns.
    """

    extraction_summary: str
    key_references: tuple[str, ...]
    has_bug_report: bool
    has_error_details: bool
    has_file_references: bool
    has_code_snippets: bool
    extracted_entities: dict[str, Any]
    inferred_cwes: tuple[str, ...]
    cwe_reasoning: dict[str, str]
    recommended_sanitizers: tuple[str, ...]
    fix_patterns: dict[str, str]
    extracted_by: UUID


# =============================================================================
# Aggregate Protocol & Base Class
# =============================================================================


class EventSourcedAggregate(Protocol):
    """Protocol for event-sourced aggregates."""

    @property
    def version(self) -> int:
        """Current version for OCC."""
        ...

    @property
    def events(self) -> list[DomainEvent]:
        """Uncommitted events pending persistence."""
        ...

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        ...


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
        pass

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
# ArtifactStore Aggregate
# =============================================================================


class ArtifactStore(EventSourcedAggregateBase):
    """Event-sourced aggregate for artifact storage.

    Stores shared outputs between agents with content type and metadata.
    """

    _artifacts: dict[str, Artifact]

    def _initialize_state(self) -> None:
        self._artifacts = {}

    def store_artifact(
        self,
        key: str,
        content_type: str,
        stored_by: UUID,
        content: str | None = None,
        content_hash: str | None = None,
        **metadata: Any,
    ) -> DomainEvent:
        """Store an artifact in shared context.

        Returns the event for the facade to collect.
        """
        return self._emit(
            ArtifactStored(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                key=key,
                content_type=content_type,
                content=content,
                content_hash=content_hash,
                stored_by=stored_by,
                metadata=dict(metadata),
            )
        )

    def get_artifact(self, key: str) -> Artifact | None:
        """Get artifact by key."""
        return self._artifacts.get(key)

    def list_artifacts(self) -> list[str]:
        """List all artifact keys."""
        return list(self._artifacts.keys())

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

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
        self._increment_version()


# =============================================================================
# DecisionLog Aggregate
# =============================================================================


class DecisionLog(EventSourcedAggregateBase):
    """Event-sourced aggregate for decision tracking.

    Records architectural and design decisions with rationale.
    """

    _decisions: dict[str, Decision]

    def _initialize_state(self) -> None:
        self._decisions = {}

    def record_decision(
        self,
        decision_key: str,
        decision_value: str,
        rationale: str,
        decided_by: UUID,
    ) -> DomainEvent:
        """Record a key decision.

        Returns the event for the facade to collect.
        """
        return self._emit(
            DecisionRecorded(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                decision_key=decision_key,
                decision_value=decision_value,
                rationale=rationale,
                decided_by=decided_by,
            )
        )

    def get_decision(self, key: str) -> Decision | None:
        """Get decision by key."""
        return self._decisions.get(key)

    def list_decisions(self) -> list[str]:
        """List all decision keys."""
        return list(self._decisions.keys())

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: DecisionRecorded) -> None:
        self._decisions[event.decision_key] = Decision(
            key=event.decision_key,
            value=event.decision_value,
            rationale=event.rationale,
            decided_by=event.decided_by,
        )
        self._increment_version()


# =============================================================================
# ProgressTracker Aggregate
# =============================================================================


class ProgressTracker(EventSourcedAggregateBase):
    """Event-sourced aggregate for progress tracking.

    Tracks progress checkpoints across the agent hierarchy.
    """

    _progress: dict[str, ProgressCheckpoint]

    def _initialize_state(self) -> None:
        self._progress = {}

    def update_progress(
        self,
        checkpoint_key: str,
        status: str,
        reported_by: UUID,
        progress_pct: float = 0.0,
        message: str = "",
    ) -> DomainEvent:
        """Update a progress checkpoint.

        Returns the event for the facade to collect.
        """
        return self._emit(
            ProgressUpdated(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                checkpoint_key=checkpoint_key,
                status=status,
                progress_pct=progress_pct,
                message=message,
                reported_by=reported_by,
            )
        )

    def get_progress(self, checkpoint_key: str) -> ProgressCheckpoint | None:
        """Get progress checkpoint by key."""
        return self._progress.get(checkpoint_key)

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: ProgressUpdated) -> None:
        self._progress[event.checkpoint_key] = ProgressCheckpoint(
            key=event.checkpoint_key,
            status=event.status,
            progress_pct=event.progress_pct,
            message=event.message,
            reported_by=event.reported_by,
        )
        self._increment_version()


# =============================================================================
# BudgetAccount Aggregate
# =============================================================================


class BudgetAccount(EventSourcedAggregateBase):
    """Event-sourced aggregate for budget tracking.

    Tracks cost consumption and enforces budget limits.
    """

    _initial_budget_usd: float
    _consumed_budget_usd: float
    _budget_exceeded: bool

    def _initialize_state(self) -> None:
        self._initial_budget_usd = 0.0
        self._consumed_budget_usd = 0.0
        self._budget_exceeded = False

    @property
    def initial_budget_usd(self) -> float:
        return self._initial_budget_usd

    @property
    def consumed_budget_usd(self) -> float:
        return self._consumed_budget_usd

    @property
    def budget_exceeded(self) -> bool:
        return self._budget_exceeded

    def initialize(self, initial_budget_usd: float) -> None:
        """Initialize budget limit (called during SharedContextCreated)."""
        self._initial_budget_usd = initial_budget_usd

    def consume_budget(
        self,
        agent_id: UUID,
        amount: float,
        operation: str,
        model: str | None = None,
        tokens: dict[str, int] | None = None,
    ) -> list[DomainEvent]:
        """Record budget consumption.

        Returns list of events (BudgetConsumed and optionally BudgetExceeded).
        """
        events: list[DomainEvent] = []

        events.append(
            self._emit(
                BudgetConsumed(
                    aggregate_id=self._aggregate_id,
                    sequence_number=self._next_sequence(),
                    consumed_by=agent_id,
                    amount_usd=amount,
                    operation=operation,
                    model=model,
                    tokens=tokens or {},
                )
            )
        )

        # Check for budget exceeded
        if self._initial_budget_usd > 0 and not self._budget_exceeded:
            if self._consumed_budget_usd >= self._initial_budget_usd:
                events.append(
                    self._emit(
                        BudgetExceeded(
                            aggregate_id=self._aggregate_id,
                            sequence_number=self._next_sequence(),
                            limit_usd=self._initial_budget_usd,
                            consumed_usd=self._consumed_budget_usd,
                            triggered_by=agent_id,
                        )
                    )
                )

        return events

    def get_remaining_budget(self) -> float:
        """Get remaining budget in USD.

        Returns -1 if unlimited (initial_budget = 0).
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

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: BudgetConsumed) -> None:
        self._consumed_budget_usd += event.amount_usd
        self._increment_version()

    @_apply.register
    def _(self, event: BudgetExceeded) -> None:
        self._budget_exceeded = True
        self._increment_version()


# =============================================================================
# ConfigOverrideStore (internal, not extracted as separate aggregate)
# =============================================================================


class ConfigOverrideStore(EventSourcedAggregateBase):
    """Internal store for configuration overrides.

    Not a full aggregate - managed by SharedExecutionContext.
    """

    _config_overrides: dict[str, ConfigOverride]

    def _initialize_state(self) -> None:
        self._config_overrides = {}

    def set_config_override(
        self,
        config_key: str,
        config_value: str,
        set_by: UUID,
        scope: str = "global",
    ) -> DomainEvent:
        """Set a configuration override."""
        return self._emit(
            ConfigOverrideSet(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                config_key=config_key,
                config_value=config_value,
                set_by=set_by,
                scope=scope,
            )
        )

    def get_config_override(self, key: str) -> ConfigOverride | None:
        """Get config override by key."""
        return self._config_overrides.get(key)

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: ConfigOverrideSet) -> None:
        self._config_overrides[event.config_key] = ConfigOverride(
            key=event.config_key,
            value=event.config_value,
            set_by=event.set_by,
            scope=event.scope,
        )
        self._increment_version()


# =============================================================================
# KnowledgeStore Aggregate (Design Choice 5)
# =============================================================================


class KnowledgeStore(EventSourcedAggregateBase):
    """Event-sourced aggregate for worker knowledge sharing (Design Choice 5).

    Stores published knowledge from completed workers for cross-worker learning.
    Thinkers publish curated worker reports to enable later workers to learn
    from earlier discoveries.
    """

    _knowledge: dict[str, PublishedKnowledge]

    def _initialize_state(self) -> None:
        self._knowledge = {}

    def publish_knowledge(
        self,
        key: str,
        objective: str,
        relevance: str,
        key_findings: tuple[str, ...],
        deliverables: tuple[str, ...],
        source_worker_id: UUID,
        published_by: UUID,
    ) -> DomainEvent:
        """Publish worker knowledge to shared context.

        Returns the event for the facade to collect.
        """
        return self._emit(
            KnowledgePublished(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                key=key,
                objective=objective,
                relevance=relevance,
                key_findings=key_findings,
                deliverables=deliverables,
                source_worker_id=source_worker_id,
                published_by=published_by,
            )
        )

    def get_knowledge(self, key: str) -> PublishedKnowledge | None:
        """Get knowledge by key."""
        return self._knowledge.get(key)

    def list_knowledge(self) -> list[str]:
        """List all knowledge keys."""
        return list(self._knowledge.keys())

    def get_all_knowledge(self) -> list[PublishedKnowledge]:
        """Get all published knowledge entries."""
        return list(self._knowledge.values())

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: KnowledgePublished) -> None:
        self._knowledge[event.key] = PublishedKnowledge(
            key=event.key,
            objective=event.objective,
            relevance=event.relevance,
            key_findings=tuple(event.key_findings),
            deliverables=tuple(event.deliverables),
            source_worker_id=event.source_worker_id,
            published_by=event.published_by,
        )
        self._increment_version()


# =============================================================================
# SourceContextStore Aggregate (Design Choice 6 & 7)
# =============================================================================


class SourceContextStore(EventSourcedAggregateBase):
    """Event-sourced aggregate for boss source context storage (Design Choice 6 & 7).

    Stores extracted key information from the boss task description including:
    - Bug summary, error messages, reproduction steps (DC6)
    - Inferred CWE patterns and fix strategies (DC7)

    This context is extracted once by BOSS and broadcast to all descendants.
    """

    _source_context: StoredSourceContext | None

    def _initialize_state(self) -> None:
        self._source_context = None

    def publish_source_context(
        self,
        extraction_summary: str,
        key_references: list[str],
        has_bug_report: bool,
        has_error_details: bool,
        has_file_references: bool,
        has_code_snippets: bool,
        extracted_entities: dict[str, Any],
        inferred_cwes: list[str],
        cwe_reasoning: dict[str, str],
        recommended_sanitizers: list[str],
        fix_patterns: dict[str, str],
        extracted_by: UUID,
    ) -> DomainEvent:
        """Publish source context to shared storage.

        Returns the event for the facade to collect.
        """
        return self._emit(
            SourceContextExtracted(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                extraction_summary=extraction_summary,
                key_references=tuple(key_references),
                has_bug_report=has_bug_report,
                has_error_details=has_error_details,
                has_file_references=has_file_references,
                has_code_snippets=has_code_snippets,
                extracted_entities=extracted_entities,
                inferred_cwes=tuple(inferred_cwes),
                cwe_reasoning=cwe_reasoning,
                recommended_sanitizers=tuple(recommended_sanitizers),
                fix_patterns=fix_patterns,
            )
        )

    def get_source_context(self) -> StoredSourceContext | None:
        """Get stored source context."""
        return self._source_context

    def has_source_context(self) -> bool:
        """Check if source context has been extracted."""
        return self._source_context is not None

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        raise TypeError(f"No handler for {type(event).__name__}")

    @_apply.register
    def _(self, event: SourceContextExtracted) -> None:
        self._source_context = StoredSourceContext(
            extraction_summary=event.extraction_summary,
            key_references=event.key_references,
            has_bug_report=event.has_bug_report,
            has_error_details=event.has_error_details,
            has_file_references=event.has_file_references,
            has_code_snippets=event.has_code_snippets,
            extracted_entities=event.extracted_entities,
            inferred_cwes=event.inferred_cwes,
            cwe_reasoning=event.cwe_reasoning,
            recommended_sanitizers=event.recommended_sanitizers,
            fix_patterns=event.fix_patterns,
            extracted_by=event.aggregate_id,  # BOSS agent ID
        )
        self._increment_version()


# =============================================================================
# SharedExecutionContext Facade
# =============================================================================


class SharedExecutionContext(EventSourcedAggregateBase):
    """Facade composing event-sourced aggregates for shared state.

    One instance per execution hierarchy (keyed by root_id).
    Composes:
    - ArtifactStore: Shared outputs between agents
    - DecisionLog: Architectural/design choices
    - ProgressTracker: Checkpoint tracking
    - BudgetAccount: Cost tracking with enforcement
    - ConfigOverrideStore: Runtime configuration
    - KnowledgeStore: Published worker knowledge (Design Choice 5)

    All state changes are event-sourced for consistency and auditability.
    """

    _root_id: UUID
    _config: dict[str, Any]
    _artifact_store: ArtifactStore
    _decision_log: DecisionLog
    _progress_tracker: ProgressTracker
    _budget_account: BudgetAccount
    _config_store: ConfigOverrideStore
    _knowledge_store: KnowledgeStore
    _source_context_store: SourceContextStore  # Design Choice 6 & 7

    def __init__(self, root_id: UUID) -> None:
        """Internal. Use create() or load_from_history() instead."""
        self._root_id = root_id
        # Derive aggregate_id before calling super().__init__
        aggregate_id = shared_context_aggregate_id(root_id)
        super().__init__(aggregate_id)

    def _initialize_state(self) -> None:
        self._config = {}
        # Composed aggregates
        self._artifact_store = ArtifactStore(self._aggregate_id)
        self._decision_log = DecisionLog(self._aggregate_id)
        self._progress_tracker = ProgressTracker(self._aggregate_id)
        self._budget_account = BudgetAccount(self._aggregate_id)
        self._config_store = ConfigOverrideStore(self._aggregate_id)
        self._knowledge_store = KnowledgeStore(self._aggregate_id)  # Design Choice 5
        self._source_context_store = SourceContextStore(self._aggregate_id)  # DC 6 & 7

    @property
    def root_id(self) -> UUID:
        """Root agent ID that this context belongs to."""
        return self._root_id

    # Note: aggregate_id, version, events inherited from EventSourcedAggregateBase

    @property
    def initial_budget_usd(self) -> float:
        """Initial budget limit (delegated to BudgetAccount)."""
        return self._budget_account.initial_budget_usd

    @property
    def consumed_budget_usd(self) -> float:
        """Total consumed budget (delegated to BudgetAccount)."""
        return self._budget_account.consumed_budget_usd

    @property
    def budget_exceeded(self) -> bool:
        """Whether budget has been exceeded (delegated to BudgetAccount)."""
        return self._budget_account.budget_exceeded

    # -------------------------------------------------------------------------
    # Factory Methods
    # -------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root_id: UUID,
        initial_budget_usd: float = 0.0,
        config: dict[str, Any] | None = None,
    ) -> "SharedExecutionContext":
        """Create new shared context for an execution run.

        Args:
            root_id: Root agent ID (becomes aggregate ID)
            initial_budget_usd: Budget limit for the run (0 = unlimited)
            config: Optional configuration dict

        Returns:
            New SharedExecutionContext with creation event
        """
        instance = cls(root_id)
        instance._emit(
            SharedContextCreated(
                aggregate_id=instance.aggregate_id,
                sequence_number=instance._next_sequence(),
                root_id=root_id,
                initial_budget_usd=initial_budget_usd,
                config=config or {},
            )
        )
        return instance

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "SharedExecutionContext":
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
            instance.set_sequence(event.sequence_number)
        return instance

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        super().mark_changes_as_committed()
        # Also clear sub-aggregate changes (they share events with facade)
        self._artifact_store.mark_changes_as_committed()
        self._decision_log.mark_changes_as_committed()
        self._progress_tracker.mark_changes_as_committed()
        self._budget_account.mark_changes_as_committed()
        self._config_store.mark_changes_as_committed()
        self._knowledge_store.mark_changes_as_committed()  # Design Choice 5
        self._source_context_store.mark_changes_as_committed()  # DC 6 & 7

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
        self._emit(
            ArtifactStored(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                key=key,
                content_type=content_type,
                content=content,
                content_hash=content_hash,
                stored_by=stored_by,
                metadata=dict(metadata),
            )
        )

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
        self._emit(
            DecisionRecorded(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                decision_key=decision_key,
                decision_value=decision_value,
                rationale=rationale,
                decided_by=decided_by,
            )
        )

    def get_decision(self, key: str) -> Decision | None:
        """Get decision by key (delegated to DecisionLog)."""
        return self._decision_log.get_decision(key)

    def list_decisions(self) -> list[str]:
        """List all decision keys (delegated to DecisionLog)."""
        return self._decision_log.list_decisions()

    # -------------------------------------------------------------------------
    # Delegated Commands - Progress
    # -------------------------------------------------------------------------

    def update_progress(
        self,
        checkpoint_key: str,
        status: str,
        reported_by: UUID,
        progress_pct: float = 0.0,
        message: str = "",
    ) -> None:
        """Update a progress checkpoint (delegated to ProgressTracker)."""
        self._emit(
            ProgressUpdated(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                checkpoint_key=checkpoint_key,
                status=status,
                progress_pct=progress_pct,
                message=message,
                reported_by=reported_by,
            )
        )

    def get_progress(self, checkpoint_key: str) -> ProgressCheckpoint | None:
        """Get progress checkpoint by key (delegated to ProgressTracker)."""
        return self._progress_tracker.get_progress(checkpoint_key)

    # -------------------------------------------------------------------------
    # Delegated Commands - Config Overrides
    # -------------------------------------------------------------------------

    def set_config_override(
        self,
        config_key: str,
        config_value: str,
        set_by: UUID,
        scope: str = "global",
    ) -> None:
        """Set a configuration override (delegated to ConfigOverrideStore)."""
        self._emit(
            ConfigOverrideSet(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                config_key=config_key,
                config_value=config_value,
                set_by=set_by,
                scope=scope,
            )
        )

    def get_config_override(self, key: str) -> ConfigOverride | None:
        """Get config override by key (delegated to ConfigOverrideStore)."""
        return self._config_store.get_config_override(key)

    # -------------------------------------------------------------------------
    # Delegated Commands - Knowledge (Design Choice 5)
    # -------------------------------------------------------------------------

    def publish_knowledge(
        self,
        key: str,
        objective: str,
        relevance: str,
        key_findings: tuple[str, ...],
        deliverables: tuple[str, ...],
        source_worker_id: UUID,
        published_by: UUID,
    ) -> None:
        """Publish worker knowledge to shared context (Design Choice 5).

        Thinkers use this to publish curated knowledge from completed workers
        to enable cross-worker learning.
        """
        self._emit(
            KnowledgePublished(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                key=key,
                objective=objective,
                relevance=relevance,
                key_findings=key_findings,
                deliverables=deliverables,
                source_worker_id=source_worker_id,
                published_by=published_by,
            )
        )

    def get_knowledge(self, key: str) -> PublishedKnowledge | None:
        """Get knowledge by key (delegated to KnowledgeStore)."""
        return self._knowledge_store.get_knowledge(key)

    def list_knowledge(self) -> list[str]:
        """List all knowledge keys (delegated to KnowledgeStore)."""
        return self._knowledge_store.list_knowledge()

    def get_all_knowledge(self) -> list[PublishedKnowledge]:
        """Get all published knowledge entries (delegated to KnowledgeStore)."""
        return self._knowledge_store.get_all_knowledge()

    # -------------------------------------------------------------------------
    # Delegated Commands - Source Context (Design Choice 6 & 7)
    # -------------------------------------------------------------------------

    def publish_source_context(
        self,
        extraction_summary: str,
        key_references: list[str],
        has_bug_report: bool,
        has_error_details: bool,
        has_file_references: bool,
        has_code_snippets: bool,
        extracted_entities: dict[str, Any],
        inferred_cwes: list[str],
        cwe_reasoning: dict[str, str],
        recommended_sanitizers: list[str],
        fix_patterns: dict[str, str],
        extracted_by: UUID,
    ) -> None:
        """Publish source context extracted from boss task (DC6 & DC7).

        BOSS extracts structured context from user input and publishes it
        to enable all descendants to access the same source information.
        """
        self._emit(
            SourceContextExtracted(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                extraction_summary=extraction_summary,
                key_references=tuple(key_references),
                has_bug_report=has_bug_report,
                has_error_details=has_error_details,
                has_file_references=has_file_references,
                has_code_snippets=has_code_snippets,
                extracted_entities=extracted_entities,
                inferred_cwes=tuple(inferred_cwes),
                cwe_reasoning=cwe_reasoning,
                recommended_sanitizers=tuple(recommended_sanitizers),
                fix_patterns=fix_patterns,
            )
        )

    def get_source_context(self) -> StoredSourceContext | None:
        """Get stored source context (delegated to SourceContextStore)."""
        return self._source_context_store.get_source_context()

    def has_source_context(self) -> bool:
        """Check if source context has been extracted."""
        return self._source_context_store.has_source_context()

    # -------------------------------------------------------------------------
    # Delegated Commands - Budget
    # -------------------------------------------------------------------------

    def consume_budget(
        self,
        agent_id: UUID,
        amount: float,
        operation: str,
        model: str | None = None,
        tokens: dict[str, int] | None = None,
    ) -> None:
        """Record budget consumption (delegated to BudgetAccount)."""
        self._emit(
            BudgetConsumed(
                aggregate_id=self._aggregate_id,
                sequence_number=self._next_sequence(),
                consumed_by=agent_id,
                amount_usd=amount,
                operation=operation,
                model=model,
                tokens=tokens or {},
            )
        )

        # Check for budget exceeded
        if self._budget_account.initial_budget_usd > 0:
            if not self._budget_account.budget_exceeded:
                if self._budget_account.consumed_budget_usd >= self._budget_account.initial_budget_usd:
                    self._emit(
                        BudgetExceeded(
                            aggregate_id=self._aggregate_id,
                            sequence_number=self._next_sequence(),
                            limit_usd=self._budget_account.initial_budget_usd,
                            consumed_usd=self._budget_account.consumed_budget_usd,
                            triggered_by=agent_id,
                        )
                    )

    def get_remaining_budget(self) -> float:
        """Get remaining budget in USD (delegated to BudgetAccount)."""
        return self._budget_account.get_remaining_budget()

    def is_budget_exceeded(self) -> bool:
        """Check if budget limit has been exceeded (delegated to BudgetAccount)."""
        return self._budget_account.is_budget_exceeded()

    def get_budget_summary(self) -> dict[str, Any]:
        """Get budget summary (delegated to BudgetAccount)."""
        return self._budget_account.get_budget_summary()

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
        self._budget_account.initialize(event.initial_budget_usd)
        self._config = event.config
        self._increment_version()

    @_apply.register
    def _(self, event: ArtifactStored) -> None:
        self._artifact_store.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: DecisionRecorded) -> None:
        self._decision_log.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: KnowledgePublished) -> None:
        self._knowledge_store.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: ProgressUpdated) -> None:
        self._progress_tracker.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: ConfigOverrideSet) -> None:
        self._config_store.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: BudgetConsumed) -> None:
        self._budget_account.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: BudgetExceeded) -> None:
        self._budget_account.apply_event(event)
        self._increment_version()

    @_apply.register
    def _(self, event: SourceContextExtracted) -> None:
        self._source_context_store.apply_event(event)
        self._increment_version()
