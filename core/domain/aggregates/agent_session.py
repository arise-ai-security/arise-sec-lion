"""Core domain models for the multi-agent system."""

from __future__ import annotations

import hashlib
from collections import deque
from functools import singledispatchmethod
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from core.domain.events.events import (
    AgentCreated,
    AgentExecutionFinished,
    AgentExecutionStarted,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DecisionInfeasible,
    DomainEvent,
    FailureDigestRecorded,
    LimitEnforced,
    OperationFinished,
    OperationStarted,
    PatchPlanApproved,
    PhaseGateRecorded,
    PhaseRouteSelected,
    ProbeCompleted,
    ProbeStarted,
    ProcedureExecutionFinished,
    ProcedureExecutionStarted,
    PromptContextAssembled,
    PromptSent,
    RedecompositionTriggered,
    RetryScheduled,
    RunCompleted,
    RunStarted,
    RuntimeSurfaceSealed,
    SealedArtifact,
    SourceFileEdited,
    SourceFileObserved,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    VerificationFailed,
    VerificationPassed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)
from core.domain.exceptions import DomainInvariantError, InvalidEventHistoryError
from core.domain.services.child_result_report import (
    aggregate_child_results,
    all_children_failed_reason,
    failed_children_note,
)
from core.domain.values.agent_config import AgentConfig
from core.domain.values.enums import AgentRole, AgentStatus
from core.domain.values.failure import ChildFailureRecord, ThoughtExcerpt
from core.domain.values.node_message import Briefing, Report, build_briefing


if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.subtask import Subtask


# Bounds for failure-context retention: the thought tail feeds the retry
# digest (~10 KB ceiling). Note/reason caps for parent aggregation notes
# live in core/domain/services/child_result_report.py.
_RECENT_THOUGHTS_MAXLEN = 20
_THOUGHT_EXCERPT_CHARS = 500


class AgentSession:
    """Event-sourced aggregate for agent sessions. State derived from replaying events."""

    agent_id: UUID
    role: AgentRole
    status: AgentStatus
    parent_id: UUID | None
    child_ids: list[UUID]
    child_reports: dict[UUID, Report]
    task_description: str
    result: str | None
    error_message: str | None
    config: AgentConfig
    version: int
    hierarchy_limits: HierarchyLimits | None
    briefing: Briefing | None
    local_decisions: list[str]
    local_artifacts: list[str]
    sibling_index: int
    success_criteria: str
    criticality: str
    dependency_failure_policy: str
    target_paths: tuple[str, ...]
    symbols: tuple[str, ...]
    search_hints: tuple[str, ...]
    estimated_complexity: str
    execution_mode: str
    procedure_ref: str
    procedure_params: dict[str, Any]
    last_attempt_procedural: bool
    retry_count: int
    redecomposition_count: int
    verification_feedback: str | None
    verification_score: int | None
    failure_digest: str | None
    recent_thoughts: deque[ThoughtExcerpt]
    failed_children: dict[UUID, ChildFailureRecord]
    child_subtasks: dict[UUID, Subtask]
    failure_history: list[ChildFailureRecord]
    # Per-coroutine reservation bookkeeping for child-spawn OCC (D.2).
    # NOT event-sourced — lives only on the in-memory aggregate for the
    # duration of one run_agent_step attempt; reset on every fresh load
    # and after the orchestrator commits or releases the reservation.
    pending_reservation: int
    _changes: list[DomainEvent]
    _sequence: int

    def __init__(self, agent_id: UUID) -> None:
        """Internal. Use AgentSession.create() or load_from_history() instead."""
        self._initialize_defaults(agent_id)

    @classmethod
    def create(
        cls,
        agent_id: UUID,
        role: AgentRole,
        config: dict[str, Any],
        parent_id: UUID | None = None,
        sibling_index: int = 0,
        briefing: dict[str, Any] | None = None,
        depends_on: list[int] | None = None,
        success_criteria: str = "",
        criticality: Literal["required", "optional"] = "required",
        dependency_failure_policy: Literal["block", "replan", "continue"] = "block",
        target_paths: list[str] | None = None,
        symbols: list[str] | None = None,
        search_hints: list[str] | None = None,
        estimated_complexity: str = "unknown",
        execution_mode: str = "auto",
        procedure_ref: str = "",
        procedure_params: dict[str, Any] | None = None,
    ) -> AgentSession:
        instance = cls(agent_id)
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=instance._next_sequence(),
            role=role.value,
            parent_id=parent_id,
            config=config,
            sibling_index=sibling_index,
            briefing=briefing,
            depends_on=depends_on or [],
            success_criteria=success_criteria,
            criticality=criticality,
            dependency_failure_policy=dependency_failure_policy,
            target_paths=target_paths or [],
            symbols=symbols or [],
            search_hints=search_hints or [],
            estimated_complexity=estimated_complexity,
            execution_mode=execution_mode,
            procedure_ref=procedure_ref,
            procedure_params=procedure_params or {},
        )
        instance._emit(event)
        return instance

    @property
    def events(self) -> list[DomainEvent]:
        """Uncommitted events pending persistence."""
        return self._changes

    def mark_changes_as_committed(self) -> None:
        self._changes.clear()

    def assign_task(self, task_description: str) -> None:
        task_event = TaskAssigned(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
        )
        self._emit(task_event)

    def handle_child_update(
        self,
        child_id: UUID,
        result: str,
        report: Report | None = None,
        *,
        max_redecompositions: int = 0,
    ) -> None:
        """For BOSS/MANAGER: record child completion, complete self when all children done.

        Args:
            child_id: ID of completed child
            result: Simple result text for the event
            report: Structured Report (optional, provides additional context)
            max_redecompositions: Re-plan budget, applied symmetrically with the
                failure path when a required sibling has already failed.
        """
        # Idempotence: a child is reported terminally at most once. Under
        # OCC retry or grandparent walks, the same notification can arrive
        # twice; the second call must no-op.
        if child_id in self.child_reports or child_id in self.failed_children:
            return

        self._assert_parent_can_receive_child_event(child_id)

        if report is None:
            report = Report.simple(result)

        child_completed_event = ChildCompleted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            result=result,
            report=report.model_dump(),
        )
        self._emit(child_completed_event)

        self._settle_if_ready(child_id, max_redecompositions=max_redecompositions)

    def handle_child_failure(
        self,
        child_id: UUID,
        reason: str,
        *,
        child_task: str = "",
        digest: str | None = None,
        max_redecompositions: int = 0,
    ) -> None:
        """For BOSS/MANAGER: record child failure, defer parent decision.

        Records the failure but waits until all children have reported
        (completed or failed) before deciding the parent's fate. If at least
        one child succeeded, the parent aggregates partial results. If ALL
        children failed, the parent re-decomposes informed by the recorded
        failures while redecomposition budget remains, otherwise fails.

        Args:
            child_id: ID of failed child
            reason: Error message from child
            child_task: The failed child's task description
            digest: The child's failure digest, when one was recorded
            max_redecompositions: Re-plan budget for the all-children-failed case
        """
        # Idempotence: a child is reported terminally at most once. Under
        # OCC retry the same failure notification can arrive twice; the
        # second call must no-op.
        if child_id in self.child_reports or child_id in self.failed_children:
            return

        self._assert_parent_can_receive_child_event(child_id)

        # Record child failure
        child_failed_event = ChildFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            reason=reason,
            child_task=child_task,
            digest=digest,
        )
        self._emit(child_failed_event)

        self._settle_if_ready(child_id, max_redecompositions=max_redecompositions)

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply event to update state. Raises TypeError for unregistered event types."""
        raise TypeError(f"No handler for {type(event).__name__}. Register with @_apply.register.")

    def _emit(self, event: DomainEvent) -> DomainEvent:
        """Apply and record an uncommitted domain event."""
        self._apply(event)
        self._changes.append(event)
        return event

    @_apply.register
    def _(self, event: AgentCreated) -> None:
        self.role = AgentRole(event.role)
        self.parent_id = event.parent_id
        adapter: TypeAdapter[AgentConfig] = TypeAdapter(AgentConfig)
        self.config = adapter.validate_python(event.config)
        self.status = AgentStatus.PENDING
        self.sibling_index = event.sibling_index
        self.success_criteria = event.success_criteria
        self.criticality = event.criticality
        self.dependency_failure_policy = event.dependency_failure_policy
        self.target_paths = tuple(event.target_paths)
        self.symbols = tuple(event.symbols)
        self.search_hints = tuple(event.search_hints)
        self.estimated_complexity = event.estimated_complexity
        self.execution_mode = event.execution_mode
        self.procedure_ref = event.procedure_ref
        self.procedure_params = dict(event.procedure_params)
        if event.briefing is not None:
            self.briefing = Briefing.model_validate(event.briefing)
        self.version += 1

    @_apply.register
    def _(self, event: TaskAssigned) -> None:
        self.task_description = event.task_description
        self.status = AgentStatus.ANALYZING
        self.version += 1

    @_apply.register
    def _(self, event: StatusChanged) -> None:
        self.status = AgentStatus(event.new_status)
        self.version += 1

    @_apply.register
    def _(self, event: SubtasksDefined) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: ChildSpawned) -> None:
        if event.child_role == AgentRole.BOSS.value:
            raise DomainInvariantError("Cannot spawn BOSS child")
        self.child_ids.append(event.child_id)
        self.child_subtasks[event.child_id] = event.subtask
        self.version += 1

    @_apply.register
    def _(self, event: WorkFailed) -> None:
        self.status = AgentStatus.FAILED
        self.error_message = event.reason
        self.version += 1

    @_apply.register
    def _(self, event: VerificationFailed) -> None:
        self.status = AgentStatus.FAILED
        self.error_message = f"Verification failed ({event.failed_stage}): {event.feedback}"
        self.verification_feedback = event.feedback
        self.verification_score = event.score
        self.version += 1

    @_apply.register
    def _(self, event: VerificationPassed) -> None:
        self.verification_score = event.score
        self.version += 1  # Observability only — status stays COMPLETED

    @_apply.register
    def _(self, event: DecisionInfeasible) -> None:
        self.status = AgentStatus.FAILED
        self.error_message = f"Infeasible: {event.reason}"
        self.version += 1

    @_apply.register
    def _(self, event: RedecompositionTriggered) -> None:
        self.status = AgentStatus.ANALYZING
        self.redecomposition_count += 1
        # Snapshot before clearing so the next decomposition prompt can
        # render every prior attempt's failures.
        self.failure_history.extend(self.failed_children.values())
        self.child_ids.clear()
        self.child_reports.clear()
        self.failed_children.clear()
        self.version += 1

    @_apply.register
    def _(self, event: ProbeStarted) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: ProbeCompleted) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: RetryScheduled) -> None:
        self.status = AgentStatus.ANALYZING
        self.retry_count = event.attempt
        self.result = None
        self.error_message = None
        if event.escalated_model:
            # AgentConfig is a Pydantic model — dump, modify, re-validate
            config_dict = self.config.model_dump()
            if "base" in config_dict and isinstance(config_dict["base"], dict):
                config_dict["base"]["model"] = event.escalated_model
            adapter: TypeAdapter[AgentConfig] = TypeAdapter(AgentConfig)
            self.config = adapter.validate_python(config_dict)
        self.version += 1

    @_apply.register
    def _(self, event: CodeGenerationStarted) -> None:
        self.status = AgentStatus.IN_PROGRESS
        self.version += 1

    @_apply.register
    def _(self, event: ProcedureExecutionStarted) -> None:
        # Once-only guard: a later failed attempt must dispatch agentic.
        self.last_attempt_procedural = True
        self.status = AgentStatus.IN_PROGRESS
        self.version += 1

    @_apply.register
    def _(self, event: ProcedureExecutionFinished) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: PhaseRouteSelected) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: PhaseGateRecorded) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: PatchPlanApproved) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: PromptContextAssembled) -> None:
        self.version += 1

    @_apply.register
    def _(self, event: ThoughtCaptured) -> None:
        self.recent_thoughts.append(
            ThoughtExcerpt(
                tool_name=event.tool_name,
                output_type=event.output_type,
                content=event.content[:_THOUGHT_EXCERPT_CHARS],
            )
        )
        self.version += 1

    @_apply.register
    def _(self, event: FailureDigestRecorded) -> None:
        self.failure_digest = event.digest
        self.version += 1

    @_apply.register
    def _(self, event: WorkCompleted) -> None:
        self.status = AgentStatus.COMPLETED
        self.result = event.result
        self.version += 1

    @_apply.register
    def _(self, event: ChildCompleted) -> None:
        if event.report:
            self.child_reports[event.child_id] = Report.model_validate(event.report)
        else:
            self.child_reports[event.child_id] = Report.simple(event.result)
        self.version += 1

    @_apply.register
    def _(self, event: ChildFailed) -> None:
        self.failed_children[event.child_id] = ChildFailureRecord(
            child_id=event.child_id,
            child_task=event.child_task,
            reason=event.reason,
            digest=event.digest,
        )
        self.version += 1

    @_apply.register
    def _(self, event: ComplexityEvaluated) -> None:
        self.role = AgentRole(event.determined_role)
        self.version += 1

    @_apply.register
    def _(self, event: TokensConsumed) -> None:
        # Cost events don't change agent state, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: LimitEnforced) -> None:
        # Limit enforcement is informational, tracks when limits affect behavior
        self.version += 1

    @_apply.register
    def _(self, event: WorkerCostRecorded) -> None:
        # Worker cost events are informational, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: PromptSent) -> None:
        # Prompt events are for observability, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: AgentExecutionStarted) -> None:
        # Timing event for observability, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: AgentExecutionFinished) -> None:
        # Timing event for observability, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: OperationStarted) -> None:
        # Timing event for observability, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: OperationFinished) -> None:
        # Timing event for observability, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: RunStarted) -> None:
        # Run-level timing event, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: RuntimeSurfaceSealed) -> None:
        # Anti-leak audit event; no state change, version for OCC.
        self.version += 1

    @_apply.register
    def _(self, event: RunCompleted) -> None:
        # Run-level timing event, just increment version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: SourceFileObserved) -> None:
        # Shared code-context capture; block rebuilt by replay, version for OCC
        self.version += 1

    @_apply.register
    def _(self, event: SourceFileEdited) -> None:
        # Shared code-context edit marker; block rebuilt by replay, version for OCC
        self.version += 1

    def _initialize_defaults(self, agent_id: UUID) -> None:
        self.agent_id = agent_id
        self.role = AgentRole.BOSS
        self.status = AgentStatus.PENDING
        self.parent_id = None
        self.child_ids = []
        self.child_reports = {}
        self.task_description = ""
        self.result = None
        self.error_message = None
        self.version = 0
        self._changes = []
        self._sequence = 0
        # Hierarchy limits for limit enforcement (set by ExecutionService)
        self.hierarchy_limits = None
        # Context passing: briefing received from parent and local state
        self.briefing = None
        self.local_decisions = []
        self.local_artifacts = []
        # Position among siblings for ordering (0 = first/leftmost)
        self.sibling_index = 0
        # Verification criteria from subtask (used by verification pipeline)
        self.success_criteria = ""
        self.criticality = "required"
        self.dependency_failure_policy = "block"
        # Structured child scoping (from parent's subtask decomposition)
        self.target_paths = ()
        self.symbols = ()
        self.search_hints = ()
        # Parent's complexity hint from the Subtask. ``"simple"`` lets the
        # orchestrator skip the assessment LLM and execute directly.
        # ``"unknown"`` (default) preserves prior behaviour.
        self.estimated_complexity = "unknown"
        # Deterministic execution tier: parent's marking from the Subtask,
        # plus the once-only guard so a failed procedure escalates agentic.
        self.execution_mode = "auto"
        self.procedure_ref = ""
        self.procedure_params = {}
        self.last_attempt_procedural = False
        # Retry tracking
        self.retry_count = 0
        self.redecomposition_count = 0
        # Verification feedback for retry (populated on VerificationFailed)
        self.verification_feedback = None
        self.verification_score = None
        # Digest of the last failed attempt (populated on FailureDigestRecorded,
        # retained across retries like verification_feedback)
        self.failure_digest = None
        # Bounded tail of worker tool output, replayed from ThoughtCaptured,
        # feeding the deterministic failure digest
        self.recent_thoughts = deque(maxlen=_RECENT_THOUGHTS_MAXLEN)
        # Track failed children for partial-success aggregation and re-planning
        self.failed_children = {}
        self.child_subtasks = {}
        # Failures accumulated across redecompositions, rendered into the
        # next decomposition prompt
        self.failure_history = []
        # Per-coroutine reservation count (see D.2). Reset on every fresh
        # load, every replay, and after commit/release in the orchestrator.
        self.pending_reservation = 0

    def set_hierarchy_limits(self, limits: HierarchyLimits) -> None:
        self.hierarchy_limits = limits

    def build_briefing_for_child(self) -> Briefing:
        """Build Briefing to pass to child agents.

        Constructs full ancestry chain.
        """
        return build_briefing(self, self.briefing)

    def record_decision(self, decision: str) -> None:
        self.local_decisions.append(decision)

    def record_artifact(self, artifact_key: str) -> None:
        self.local_artifacts.append(artifact_key)

    def build_report(self) -> Report:
        execution_summary: dict[str, Any] = {}
        total_cost = 0.0
        for event in self._changes:
            if isinstance(event, TokensConsumed):
                total_cost += event.cost_usd
        if total_cost > 0:
            execution_summary["cost_usd"] = total_cost

        return Report(
            task=self.task_description or "",
            result=self.result or "",
            artifacts=tuple(self.local_artifacts),
            decisions=tuple(self.local_decisions),
            execution_summary=execution_summary,
        )

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> AgentSession:
        """Reconstruct AgentSession by replaying events."""
        if not events:
            raise InvalidEventHistoryError("Cannot load from empty event history")
        first_event = events[0]
        if not isinstance(first_event, AgentCreated):
            raise InvalidEventHistoryError(
                f"First event must be AgentCreated, got {type(first_event).__name__}"
            )

        instance = cls(first_event.aggregate_id)
        for event in events:
            instance._apply(event)
            instance._sequence = max(instance._sequence, event.sequence_number)
        return instance

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _assert_parent_can_receive_child_event(self, child_id: UUID) -> None:
        if self.role not in (AgentRole.MANAGER, AgentRole.BOSS):
            raise DomainInvariantError(f"Requires BOSS/MANAGER, got {self.role}")
        if not self.child_ids:
            raise DomainInvariantError("Requires agent with children")
        if self.status != AgentStatus.WAITING:
            raise DomainInvariantError(
                f"Requires WAITING status, got {self.status}"
            )
        if child_id not in self.child_ids:
            raise DomainInvariantError(
                f"child_id {child_id} not in spawned children"
            )

    def _blocked_child_ids(self) -> set[UUID]:
        """Unreported children that can never run because a hard dependency
        terminally failed (or is itself blocked) under a non-"continue" policy.

        ``child_ids`` is in sibling-index order, so a subtask's ``depends_on``
        indices map positionally onto it. Computed to a fixpoint so a chain
        A(failed) -> B -> C blocks both B and C.
        """
        unsatisfiable: set[UUID] = set(self.failed_children)
        changed = True
        while changed:
            changed = False
            for child_id in self.child_ids:
                if child_id in unsatisfiable or child_id in self.child_reports:
                    continue
                subtask = self.child_subtasks.get(child_id)
                if subtask is None or subtask.dependency_failure_policy == "continue":
                    continue
                for dep_index in subtask.depends_on:
                    if 0 <= dep_index < len(self.child_ids):
                        if self.child_ids[dep_index] in unsatisfiable:
                            unsatisfiable.add(child_id)
                            changed = True
                            break
        return unsatisfiable - set(self.failed_children)

    def _required_children_unsatisfiable(self) -> bool:
        """A required child has terminally failed or is permanently blocked.

        Either means the parent can no longer produce a successful outcome, so
        a ``WorkCompleted`` must not be emitted for it (self-healing/failure
        takes over instead).
        """
        candidates = set(self.failed_children) | self._blocked_child_ids()
        return any(
            self.child_subtasks[child_id].criticality == "required"
            for child_id in candidates
            if child_id in self.child_subtasks
        )

    def _settle_if_ready(self, trigger_child_id: UUID, *, max_redecompositions: int) -> None:
        """Decide the parent's outcome once every child is settled.

        A child is settled when it has completed, failed, or is permanently
        blocked by a failed hard dependency. Blocked children never report, so
        waiting for them would stall the parent until the run watchdog fires;
        counting them as settled lets a failed required producer drive the
        replan/fail decision immediately.
        """
        blocked = self._blocked_child_ids()
        settled = len(self.child_reports) + len(self.failed_children) + len(blocked)
        if settled < len(self.child_ids):
            return  # a still-runnable child has not reported yet

        if self._required_children_unsatisfiable():
            self._handle_required_child_failure(
                trigger_child_id, max_redecompositions=max_redecompositions
            )
            return
        if self.child_reports:
            aggregated_result = aggregate_child_results(self.child_ids, self.child_reports)
            if self.failed_children:
                aggregated_result += f"\n\n{failed_children_note(self.failed_children)}"
            self._emit(
                WorkCompleted(
                    aggregate_id=self.agent_id,
                    sequence_number=self._next_sequence(),
                    result=aggregated_result,
                )
            )
            return
        if self.redecomposition_count < max_redecompositions:
            self.trigger_redecomposition(
                trigger_child_id=trigger_child_id,
                reason=all_children_failed_reason(self.failed_children),
            )
            return
        self._emit(
            WorkFailed(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                reason=all_children_failed_reason(self.failed_children),
            )
        )

    def _handle_required_child_failure(
        self,
        trigger_child_id: UUID,
        *,
        max_redecompositions: int,
    ) -> None:
        if len(self.failed_children) == len(self.child_ids):
            reason = all_children_failed_reason(self.failed_children)
        else:
            reason = f"Required child failed. {failed_children_note(self.failed_children)}"
        if self.redecomposition_count < max_redecompositions:
            self.trigger_redecomposition(trigger_child_id=trigger_child_id, reason=reason)
            return
        self._emit(
            WorkFailed(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                reason=reason,
            )
        )

    def is_terminal(self) -> bool:
        """True if COMPLETED or FAILED."""
        return self.status in (AgentStatus.COMPLETED, AgentStatus.FAILED)

    def is_leaf(self) -> bool:
        """True if WORKER with no children."""
        return self.role == AgentRole.WORKER and len(self.child_ids) == 0

    def mark_infeasible(
        self,
        reason: str,
        minimum_subtasks: int | None = None,
        minimum_depth: int | None = None,
    ) -> None:
        event = DecisionInfeasible(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            reason=reason,
            minimum_subtasks=minimum_subtasks,
            minimum_depth=minimum_depth,
        )
        self._emit(event)

    def trigger_redecomposition(self, trigger_child_id: UUID, reason: str) -> None:
        """Re-decompose after child signals infeasible. WAITING → ANALYZING."""
        event = RedecompositionTriggered(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            trigger_child_id=trigger_child_id,
            reason=reason,
        )
        self._emit(event)

    def schedule_retry(self, reason: str, escalated_model: str | None = None) -> None:
        """Schedule retry, transitioning FAILED → ANALYZING."""
        event = RetryScheduled(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            attempt=self.retry_count + 1,
            reason=reason,
            escalated_model=escalated_model,
        )
        self._emit(event)

    def record_failure_digest(self, digest: str, source: str) -> None:
        """Record a deterministic digest of the failed attempt for retry prompts."""
        event = FailureDigestRecorded(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            digest=digest,
            source=source,
        )
        self._emit(event)

    def start_procedure(self, procedure_ref: str) -> None:
        """WORKER begins a deterministic procedure (no prompt, no LLM)."""
        event = ProcedureExecutionStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            procedure_ref=procedure_ref,
        )
        self._emit(event)

    def finish_procedure(
        self,
        procedure_ref: str,
        success: bool,
        summary: str,
        evidence: list[dict[str, Any]],
    ) -> None:
        """Record the host-captured procedure outcome; terminal event follows."""
        event = ProcedureExecutionFinished(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            procedure_ref=procedure_ref,
            success=success,
            summary=summary,
            evidence=evidence,
        )
        self._emit(event)

    def record_phase_route(
        self,
        *,
        policy_version: str,
        phase: str,
        route: Literal["compact", "expanded", "escalated"],
        evidence_references: list[str],
        selected_roles: list[str],
        triggers: list[str],
        remaining_budget: int,
    ) -> None:
        self._emit(
            PhaseRouteSelected(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                policy_version=policy_version,
                phase=phase,
                route=route,
                evidence_references=evidence_references,
                selected_roles=selected_roles,
                triggers=triggers,
                remaining_budget=remaining_budget,
            )
        )

    def record_prompt_context(
        self,
        *,
        total_chars: int,
        total_tokens: int,
        segment_sizes: dict[str, int],
        entries: list[dict[str, Any]],
        omitted_entries: list[dict[str, Any]],
    ) -> None:
        self._emit(
            PromptContextAssembled(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                total_chars=total_chars,
                total_tokens=total_tokens,
                segment_sizes=segment_sizes,
                entries=entries,
                omitted_entries=omitted_entries,
            )
        )

    def record_phase_gate(
        self,
        *,
        phase: str,
        passed: bool,
        evidence_references: list[str],
        reason: str = "",
    ) -> None:
        self._emit(
            PhaseGateRecorded(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                phase=phase,
                passed=passed,
                evidence_references=evidence_references,
                reason=reason,
            )
        )

    def record_patch_plan_approved(
        self,
        *,
        plan_sha256: str,
        evidence_references: list[str],
    ) -> None:
        """Record that a host-validated security PatchPlan was frozen and approved.

        Emitted after plan validation passes and before the weak Patch-Applier
        runs, so the frozen plan identity is on the event stream (provenance the
        agent cannot forge).
        """
        self._emit(
            PatchPlanApproved(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                plan_sha256=plan_sha256,
                evidence_references=evidence_references,
            )
        )

    def fail_with_reason(self, reason: str) -> None:
        failed_event = WorkFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            reason=reason,
        )
        self._emit(failed_event)

    def complete_with_result(self, result: str) -> None:
        """Complete directly with a host-computed result (procedural tier)."""
        completed_event = WorkCompleted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            result=result,
        )
        self._emit(completed_event)


    def apply_complexity_result(
        self,
        complexity: str,
        reasoning: str,
        determined_role: AgentRole,
    ) -> None:
        """Apply complexity evaluation result (pure domain method).

        Called by orchestrator after LLM call completes.
        """
        complexity_event = ComplexityEvaluated(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            complexity=complexity,
            determined_role=determined_role.value,
            reasoning=reasoning,
        )
        self._emit(complexity_event)

    def emit_tokens_consumed(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_usd: float,
        operation: str,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Emit TokensConsumed event (pure domain method).

        Called by orchestrator after LLM call completes.
        """
        cost_event = TokensConsumed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=cost_usd,
            operation=operation,
        )
        self._emit(cost_event)

    def emit_limit_enforced(
        self,
        limit_type: str,
        limit_value: int,
        attempted_value: int,
        action_taken: str,
    ) -> None:
        limit_event = LimitEnforced(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            limit_type=limit_type,
            limit_value=limit_value,
            attempted_value=attempted_value,
            action_taken=action_taken,
        )
        self._emit(limit_event)

    def emit_prompt_sent(
        self,
        prompt: str,
        prompt_type: str,
        target: str,
    ) -> None:
        """Emit PromptSent event for observability (pure domain method).

        Called by orchestrator before LLM/worker calls to capture prompts.

        Args:
            prompt: The full prompt text being sent.
            prompt_type: Type of prompt
                (complexity_evaluation, task_decomposition, worker_execution).
            target: Where prompt is sent (llm, claude_code, openhands, etc.).
        """
        prompt_event = PromptSent(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            prompt=prompt,
            prompt_type=prompt_type,
            target=target,
        )
        self._emit(prompt_event)

    def apply_subtasks_and_spawn_children(
        self,
        subtasks: list,
        child_role: str,
        briefing: Briefing,
    ) -> list[tuple[UUID, Any]]:
        """Apply subtask decomposition and spawn children (pure domain method).

        Returns list of (child_id, subtask) tuples for the orchestrator to create.
        """
        self._emit_subtasks_defined(subtasks)
        children_to_spawn = self._emit_child_spawns(
            subtasks=subtasks,
            child_role=child_role,
            briefing=briefing,
        )
        self._emit_waiting_for_children()

        return children_to_spawn

    def _emit_subtasks_defined(self, subtasks: list[Any]) -> None:
        """Record the decomposition result before spawning children."""
        subtasks_event = SubtasksDefined(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            subtasks=subtasks,
        )
        self._emit(subtasks_event)

    def _emit_child_spawns(
        self,
        *,
        subtasks: list[Any],
        child_role: str,
        briefing: Briefing,
    ) -> list[tuple[UUID, Subtask]]:
        children_to_spawn: list[tuple[UUID, Subtask]] = []

        for sibling_index, subtask in enumerate(subtasks):
            child_id = uuid4()
            # Augment briefing with per-subtask justification (Design Choice 4).
            child_briefing = (
                briefing.model_copy(
                    update={"subtask_justification": subtask.justification}
                )
                if subtask.justification
                else briefing
            )
            child_event = ChildSpawned(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                child_id=child_id,
                child_role=child_role,
                subtask=subtask,
                child_config=subtask.config,
                briefing=child_briefing.model_dump(),
                sibling_index=sibling_index,
            )
            self._emit(child_event)
            children_to_spawn.append((child_id, subtask))

        return children_to_spawn

    def _emit_waiting_for_children(self) -> None:
        """Transition the parent into WAITING after child creation."""
        status_event = StatusChanged(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            old_status=self.status.value,
            new_status=AgentStatus.WAITING.value,
            reason="Decomposed task, waiting for child agents",
        )
        self._emit(status_event)

    def start_worker_execution(self, tool_name: str) -> None:
        """Emit CodeGenerationStarted event (pure domain method)."""
        started_event = CodeGenerationStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            tool_name=tool_name,
        )
        self._emit(started_event)

    def apply_worker_event(self, tool_event: DomainEvent) -> None:
        event_data = tool_event.model_dump(exclude={"aggregate_id", "sequence_number"})
        event_data["aggregate_id"] = self.agent_id
        event_data["sequence_number"] = self._next_sequence()
        corrected_event = type(tool_event)(**event_data)
        self._emit(corrected_event)

    def record_source_file_observed(
        self,
        path: str,
        content: str,
        observed_by: UUID,
        *,
        capture_type: Literal["full", "range", "truncated"] = "truncated",
        requested_start_line: int | None = None,
        requested_end_line: int | None = None,
        actual_start_line: int | None = None,
        actual_end_line: int | None = None,
        phase: str = "",
        role: str = "",
    ) -> None:
        """Persist verbatim source a worker's view tool returned.

        The content (and its sha256 for integrity/dedup) is recoverable from
        this event alone, backing the shared code-prefix block.
        """
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        event = SourceFileObserved(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            path=path,
            content=content,
            content_sha256=content_sha256,
            observed_by=str(observed_by),
            capture_type=capture_type,
            requested_start_line=requested_start_line,
            requested_end_line=requested_end_line,
            actual_start_line=actual_start_line,
            actual_end_line=actual_end_line,
            file_revision=content_sha256,
            phase=phase,
            role=role,
        )
        self._emit(event)

    def record_source_file_edited(self, path: str, edited_by: UUID) -> None:
        """Mark a source file edited, invalidating its recorded content."""
        event = SourceFileEdited(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            path=path,
            edited_by=str(edited_by),
        )
        self._emit(event)

    def mark_verification_failed(
        self,
        failed_stage: str,
        feedback: str,
        stages_passed: list[str],
        score: int = 0,
    ) -> None:
        """Record a verification failure via the public aggregate API."""
        event = VerificationFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            failed_stage=failed_stage,
            feedback=feedback,
            stages_passed=stages_passed,
            score=score,
        )
        self._emit(event)

    def mark_verification_passed(self, feedback: str = "", score: int = 100) -> None:
        event = VerificationPassed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            feedback=feedback,
            score=score,
        )
        self._emit(event)

    # -------------------------------------------------------------------------
    # Timing/Observability Events
    # -------------------------------------------------------------------------

    def emit_execution_started(self, role: str, depth: int) -> None:
        event = AgentExecutionStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            role=role,
            depth=depth,
        )
        self._emit(event)

    def emit_execution_finished(
        self, role: str, status: str, duration_seconds: float
    ) -> None:
        event = AgentExecutionFinished(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            role=role,
            status=status,
            duration_seconds=duration_seconds,
        )
        self._emit(event)

    def emit_operation_started(self, operation_type: str) -> None:
        event = OperationStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            operation_type=operation_type,
        )
        self._emit(event)

    def emit_operation_finished(
        self, operation_type: str, duration_seconds: float
    ) -> None:
        event = OperationFinished(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            operation_type=operation_type,
            duration_seconds=duration_seconds,
        )
        self._emit(event)

    # -------------------------------------------------------------------------
    # Probe Events (reconnaissance tool calls)
    # -------------------------------------------------------------------------

    def emit_probe_started(self, probe_type: str) -> None:
        event = ProbeStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            probe_type=probe_type,
        )
        self._emit(event)

    def emit_probe_completed(
        self, probe_type: str, result_summary: str = ""
    ) -> None:
        event = ProbeCompleted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            probe_type=probe_type,
            result_summary=result_summary,
        )
        self._emit(event)

    # -------------------------------------------------------------------------
    # Run-Level Timing Events (BOSS only)
    # -------------------------------------------------------------------------

    def emit_run_started(
        self,
        task_description: str,
        domain_metadata: dict[str, str | int | float | bool | None] | None = None,
        treatment_version: str | None = None,
        config_hash: str | None = None,
    ) -> None:
        """Emit RunStarted event when execution run begins.

        Should only be called on BOSS agent (root of hierarchy).
        """
        event = RunStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
            domain_metadata=domain_metadata,
            treatment_version=treatment_version,
            config_hash=config_hash,
        )
        self._emit(event)

    def emit_runtime_surface_sealed(
        self,
        surface: str,
        sealed_artifacts: list[SealedArtifact],
    ) -> None:
        """Emit RuntimeSurfaceSealed when Arise seals the agent-visible runtime surface.

        Should only be called on the BOSS agent (root of hierarchy).
        """
        event = RuntimeSurfaceSealed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            surface=surface,
            sealed_artifacts=sealed_artifacts,
        )
        self._emit(event)

    def emit_run_completed(
        self,
        status: str,
        duration_seconds: float,
        total_agents: int,
        completed_agents: int,
        failed_agents: int,
    ) -> None:
        """Emit RunCompleted event when execution run finishes.

        Should only be called on BOSS agent (root of hierarchy).
        """
        event = RunCompleted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            status=status,
            duration_seconds=duration_seconds,
            total_agents=total_agents,
            completed_agents=completed_agents,
            failed_agents=failed_agents,
        )
        self._emit(event)
