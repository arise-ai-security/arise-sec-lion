"""Core domain models for the multi-agent system."""

from __future__ import annotations

from functools import singledispatchmethod
from typing import TYPE_CHECKING, Any
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
    LimitEnforced,
    OperationFinished,
    OperationStarted,
    ProbeCompleted,
    ProbeStarted,
    PromptSent,
    RedecompositionTriggered,
    RetryScheduled,
    RunCompleted,
    RunStarted,
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
from core.domain.values.agent_config import AgentConfig
from core.domain.values.enums import AgentRole, AgentStatus
from core.domain.values.node_message import Briefing, Report, build_briefing


if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.subtask import Subtask


class AgentSession:
    """Event-sourced aggregate for agent sessions. State derived from replaying events."""

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
        target_paths: list[str] | None = None,
        symbols: list[str] | None = None,
        search_hints: list[str] | None = None,
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
            target_paths=target_paths or [],
            symbols=symbols or [],
            search_hints=search_hints or [],
        )
        instance._emit(event)
        return instance

    @property
    def events(self) -> list[DomainEvent]:
        """Uncommitted events pending persistence."""
        return self._changes

    def mark_changes_as_committed(self) -> None:
        """Clear uncommitted changes after successful persistence."""
        self._changes.clear()

    def assign_task(self, task_description: str) -> None:
        """Assign task, transitions to ANALYZING status."""
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
    ) -> None:
        """For BOSS/MANAGER: record child completion, complete self when all children done.

        Args:
            child_id: ID of completed child
            result: Simple result text for the event
            report: Structured Report (optional, provides additional context)
        """
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

        reported = len(self.child_reports) + len(self.failed_children)
        if reported == len(self.child_ids):
            aggregated_result = self._aggregate_child_results()
            if self.failed_children:
                failed_summary = ", ".join(
                    str(cid)[:8] for cid in self.failed_children
                )
                aggregated_result += (
                    f"\n\nNote: {len(self.failed_children)} child(ren) failed "
                    f"({failed_summary}). Results are partial."
                )
            work_completed_event = WorkCompleted(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                result=aggregated_result,
            )
            self._emit(work_completed_event)

    def _aggregate_child_results(self) -> str:
        """Combine results from all completed children using structured Reports."""
        lines = ["Subtask results:", ""]
        for child_id in self.child_ids:
            report = self.child_reports.get(child_id)
            if report is None:
                lines.append("- No result")
                continue
            header = f"[{report.task}]" if report.task else "[subtask]"
            lines.append(f"- {header}: {report.result}")
            if report.artifacts:
                lines.append(f"  Artifacts: {', '.join(report.artifacts)}")
            if report.decisions:
                lines.append(f"  Decisions: {', '.join(report.decisions)}")
        return "\n".join(lines)

    def handle_child_failure(self, child_id: UUID, reason: str) -> None:
        """For BOSS/MANAGER: record child failure, defer parent decision.

        Records the failure but waits until all children have reported
        (completed or failed) before deciding the parent's fate. If at least
        one child succeeded, the parent aggregates partial results. If ALL
        children failed, the parent fails.

        Args:
            child_id: ID of failed child
            reason: Error message from child
        """
        self._assert_parent_can_receive_child_event(child_id)

        # Record child failure
        child_failed_event = ChildFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            reason=reason,
        )
        self._emit(child_failed_event)

        # Check if all children have now reported (completed or failed)
        reported = len(self.child_reports) + len(self.failed_children)
        if reported < len(self.child_ids):
            return  # Still waiting for other children

        # All children reported — decide parent outcome
        if self.child_reports:
            # At least one child succeeded — aggregate partial results
            aggregated = self._aggregate_child_results()
            failed_summary = ", ".join(
                str(cid)[:8] for cid in self.failed_children
            )
            result = (
                f"{aggregated}\n\n"
                f"Note: {len(self.failed_children)} child(ren) failed "
                f"({failed_summary}). Results are partial."
            )
            work_completed_event = WorkCompleted(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                result=result,
            )
            self._emit(work_completed_event)
        else:
            # ALL children failed — parent fails
            work_failed_event = WorkFailed(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                reason=f"All children failed. Last: {reason}",
            )
            self._emit(work_failed_event)

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
        adapter = TypeAdapter(AgentConfig)
        self.config = adapter.validate_python(event.config)
        self.status = AgentStatus.PENDING
        self.sibling_index = event.sibling_index
        self.success_criteria = event.success_criteria
        self.target_paths = tuple(event.target_paths)
        self.symbols = tuple(event.symbols)
        self.search_hints = tuple(event.search_hints)
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
        self.version += 1

    @_apply.register
    def _(self, event: VerificationPassed) -> None:
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
            adapter = TypeAdapter(AgentConfig)
            self.config = adapter.validate_python(config_dict)
        self.version += 1

    @_apply.register
    def _(self, event: CodeGenerationStarted) -> None:
        self.status = AgentStatus.IN_PROGRESS
        self.version += 1

    @_apply.register
    def _(self, event: ThoughtCaptured) -> None:
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
        self.failed_children.add(event.child_id)
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
    def _(self, event: RunCompleted) -> None:
        # Run-level timing event, just increment version for OCC
        self.version += 1

    def _initialize_defaults(self, agent_id: UUID) -> None:
        self.agent_id: UUID = agent_id
        self.role: AgentRole = AgentRole.BOSS
        self.status: AgentStatus = AgentStatus.PENDING
        self.parent_id: UUID | None = None
        self.child_ids: list[UUID] = []
        self.child_reports: dict[UUID, Report] = {}
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.config: AgentConfig
        self.version: int = 0
        self._changes: list[DomainEvent] = []
        self._sequence: int = 0
        # Hierarchy limits for limit enforcement (set by ExecutionService)
        self.hierarchy_limits: HierarchyLimits | None = None
        # Context passing: briefing received from parent and local state
        self.briefing: Briefing | None = None
        self.local_decisions: list[str] = []
        self.local_artifacts: list[str] = []
        # Position among siblings for ordering (0 = first/leftmost)
        self.sibling_index: int = 0
        # Verification criteria from subtask (used by verification pipeline)
        self.success_criteria: str = ""
        # Structured child scoping (from parent's subtask decomposition)
        self.target_paths: tuple[str, ...] = ()
        self.symbols: tuple[str, ...] = ()
        self.search_hints: tuple[str, ...] = ()
        # Retry tracking
        self.retry_count: int = 0
        self.redecomposition_count: int = 0
        # Verification feedback for retry (populated on VerificationFailed)
        self.verification_feedback: str | None = None
        # Track failed children for partial-success aggregation
        self.failed_children: set[UUID] = set()

    def set_hierarchy_limits(self, limits: HierarchyLimits) -> None:
        """Set hierarchy limits for limit enforcement."""
        self.hierarchy_limits = limits

    def build_briefing_for_child(self) -> Briefing:
        """Build Briefing to pass to child agents.

        Constructs full ancestry chain.
        """
        return build_briefing(self, self.briefing)

    def record_decision(self, decision: str) -> None:
        """Record a local decision made during execution."""
        self.local_decisions.append(decision)

    def record_artifact(self, artifact_key: str) -> None:
        """Record an artifact produced during execution."""
        self.local_artifacts.append(artifact_key)

    def build_report(self) -> Report:
        """Build structured Report to return to parent."""
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
        """Validate parent state before applying a child completion/failure event."""
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
        """Mark task as infeasible within given constraints."""
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

    def fail_with_reason(self, reason: str) -> None:
        """Mark agent as failed with WorkFailed event."""
        failed_event = WorkFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            reason=reason,
        )
        self._emit(failed_event)

    # -------------------------------------------------------------------------
    # Pure Domain Methods (event emission only, no I/O)
    # These are called by the orchestrator after it performs LLM/worker calls.
    # -------------------------------------------------------------------------

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
        """Emit LimitEnforced event (pure domain method)."""
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
        """Emit child spawn events for each generated subtask."""
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
        """Apply a worker tool event with corrected sequence number."""
        event_data = tool_event.model_dump(exclude={"aggregate_id", "sequence_number"})
        event_data["aggregate_id"] = self.agent_id
        event_data["sequence_number"] = self._next_sequence()
        corrected_event = type(tool_event)(**event_data)
        self._emit(corrected_event)

    def mark_verification_failed(
        self,
        failed_stage: str,
        feedback: str,
        stages_passed: list[str],
    ) -> None:
        """Record a verification failure via the public aggregate API."""
        event = VerificationFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            failed_stage=failed_stage,
            feedback=feedback,
            stages_passed=stages_passed,
        )
        self._emit(event)

    def mark_verification_passed(self, feedback: str = "") -> None:
        """Record that verification passed (observability event)."""
        event = VerificationPassed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            feedback=feedback,
        )
        self._emit(event)

    # -------------------------------------------------------------------------
    # Timing/Observability Events
    # -------------------------------------------------------------------------

    def emit_execution_started(self, role: str, depth: int) -> None:
        """Emit AgentExecutionStarted event for timing observability."""
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
        """Emit AgentExecutionFinished event for timing observability."""
        event = AgentExecutionFinished(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            role=role,
            status=status,
            duration_seconds=duration_seconds,
        )
        self._emit(event)

    def emit_operation_started(self, operation_type: str) -> None:
        """Emit OperationStarted event for timing observability."""
        event = OperationStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            operation_type=operation_type,
        )
        self._emit(event)

    def emit_operation_finished(
        self, operation_type: str, duration_seconds: float
    ) -> None:
        """Emit OperationFinished event for timing observability."""
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
        """Emit ProbeStarted event for tool-calling observability."""
        event = ProbeStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            probe_type=probe_type,
        )
        self._emit(event)

    def emit_probe_completed(
        self, probe_type: str, result_summary: str = ""
    ) -> None:
        """Emit ProbeCompleted event for tool-calling observability."""
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
    ) -> None:
        """Emit RunStarted event when execution run begins.

        Should only be called on BOSS agent (root of hierarchy).
        """
        event = RunStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
            domain_metadata=domain_metadata,
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
