"""Core domain models for the multi-agent system."""

from functools import singledispatchmethod
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from core.domain.values.agent_config import AgentConfig
from core.domain.values.context import (
    HierarchyLimits,
    SpawnPayload,
    TaskOutcome,
    build_spawn_payload,
)
from core.domain.values.enums import AgentRole, AgentStatus
from core.domain.events.events import (
    AgentCreated,
    AgentExecutionFinished,
    AgentExecutionStarted,
    ChildCompleted,
    ChildFailed,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DomainEvent,
    LimitEnforced,
    OperationFinished,
    OperationStarted,
    PromptSent,
    RunCompleted,
    RunStarted,
    StatusChanged,
    SubtasksDefined,
    TaskAssigned,
    ThoughtCaptured,
    TokensConsumed,
    WorkCompleted,
    WorkerCostRecorded,
    WorkFailed,
)


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
        spawn_payload: dict[str, Any] | None = None,
    ) -> "AgentSession":
        instance = cls(agent_id)
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=instance._next_sequence(),
            role=role.value,
            parent_id=parent_id,
            config=config,
            sibling_index=sibling_index,
            spawn_payload=spawn_payload,
        )
        instance._apply(event)
        instance._changes.append(event)
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
        self._apply(task_event)
        self._changes.append(task_event)

    def handle_child_update(
        self,
        child_id: UUID,
        result: str,
        task_outcome: TaskOutcome | None = None,
    ) -> None:
        """For BOSS/MANAGER: record child completion, complete self when all children done.

        Args:
            child_id: ID of completed child
            result: Simple result text for the event
            task_outcome: Structured TaskOutcome (optional, provides additional context)
        """
        assert self.role in (AgentRole.MANAGER, AgentRole.BOSS), (
            f"Requires BOSS/MANAGER, got {self.role}"
        )
        assert len(self.child_ids) > 0, "Requires agent with children"
        assert self.status == AgentStatus.WAITING, f"Requires WAITING status, got {self.status}"
        assert child_id in self.child_ids, f"child_id {child_id} not in spawned children"

        # Use structured result if provided, otherwise create simple one
        if task_outcome is None:
            task_outcome = TaskOutcome.simple(result)

        # NOTE: structured_task_outcomes is populated in _apply(ChildCompleted)
        # to maintain event sourcing invariant: state only changes through events

        child_completed_event = ChildCompleted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            result=result,
            child_result=task_outcome.model_dump(),
        )
        self._apply(child_completed_event)
        self._changes.append(child_completed_event)

        if len(self.child_results) == len(self.child_ids):
            aggregated_result = self._aggregate_child_results()
            work_completed_event = WorkCompleted(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                result=aggregated_result,
            )
            self._apply(work_completed_event)
            self._changes.append(work_completed_event)

    def _aggregate_child_results(self) -> str:
        """Combine results from all completed children."""
        lines = ["All subtasks completed successfully:", ""]
        for child_id in self.child_ids:
            child_result = self.child_results.get(child_id, "No result")
            lines.append(f"- {child_result}")
        return "\n".join(lines)

    def handle_child_failure(self, child_id: UUID, reason: str) -> None:
        """For BOSS/MANAGER: record child failure and propagate failure up.

        When any child fails, the parent also fails. This ensures failures
        bubble up to the BOSS so the execution doesn't hang in WAITING state.

        Args:
            child_id: ID of failed child
            reason: Error message from child
        """
        assert self.role in (AgentRole.MANAGER, AgentRole.BOSS), (
            f"Requires BOSS/MANAGER, got {self.role}"
        )
        assert len(self.child_ids) > 0, "Requires agent with children"
        assert self.status == AgentStatus.WAITING, f"Requires WAITING status, got {self.status}"
        assert child_id in self.child_ids, f"child_id {child_id} not in spawned children"

        # Record child failure
        child_failed_event = ChildFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            reason=reason,
        )
        self._apply(child_failed_event)
        self._changes.append(child_failed_event)

        # Parent also fails when any child fails
        work_failed_event = WorkFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            reason=f"Child agent {child_id} failed: {reason}",
        )
        self._apply(work_failed_event)
        self._changes.append(work_failed_event)

    @singledispatchmethod
    def _apply(self, event: Any) -> None:
        """Apply event to update state. Raises TypeError for unregistered event types."""
        raise TypeError(f"No handler for {type(event).__name__}. Register with @_apply.register.")

    @_apply.register
    def _(self, event: AgentCreated) -> None:
        self.role = AgentRole(event.role)
        self.parent_id = event.parent_id
        adapter = TypeAdapter(AgentConfig)
        self.config = adapter.validate_python(event.config)
        self.status = AgentStatus.PENDING
        self.sibling_index = event.sibling_index
        # Restore spawn_payload from event if present
        if event.spawn_payload is not None:
            self.spawn_payload = SpawnPayload.model_validate(event.spawn_payload)
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
        assert event.child_role != AgentRole.BOSS.value, "Cannot spawn BOSS child"
        self.child_ids.append(event.child_id)
        self.version += 1

    @_apply.register
    def _(self, event: WorkFailed) -> None:
        self.status = AgentStatus.FAILED
        self.error_message = event.reason
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
        self.child_results[event.child_id] = event.result
        # Reconstruct structured result from event data
        if event.child_result:
            self.structured_task_outcomes[event.child_id] = TaskOutcome.model_validate(
                event.child_result
            )
        self.version += 1

    @_apply.register
    def _(self, event: ChildFailed) -> None:
        # Track failed children (could extend to store failure reasons if needed)
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
        self.child_results: dict[UUID, str] = {}
        self.task_description: str = ""
        self.result: str | None = None
        self.error_message: str | None = None
        self.config: AgentConfig
        self.version: int = 0
        self._changes: list[DomainEvent] = []
        self._sequence: int = 0
        # Hierarchy limits for limit enforcement (set by ExecutionService)
        self.hierarchy_limits: HierarchyLimits | None = None
        # Context passing: spawn payload received from parent and local state
        self.spawn_payload: SpawnPayload | None = None
        self.local_decisions: list[str] = []
        self.local_artifacts: list[str] = []
        # Structured task outcomes from children (keyed by child_id)
        self.structured_task_outcomes: dict[UUID, TaskOutcome] = {}
        # Position among siblings for left-to-right ordering (0 = first/leftmost)
        self.sibling_index: int = 0

    def set_hierarchy_limits(self, limits: HierarchyLimits) -> None:
        """Set hierarchy limits for limit enforcement."""
        self.hierarchy_limits = limits

    def get_spawn_payload_for_child(self) -> SpawnPayload:
        """Build spawn payload to pass to child agents.

        Constructs full ancestry chain and execution limits.
        """
        return build_spawn_payload(self, self.spawn_payload)

    def record_decision(self, decision: str) -> None:
        """Record a local decision made during execution."""
        self.local_decisions.append(decision)

    def record_artifact(self, artifact_key: str) -> None:
        """Record an artifact produced during execution."""
        self.local_artifacts.append(artifact_key)

    def build_task_outcome(self) -> TaskOutcome:
        """Build structured task outcome to return to parent.

        Includes task summary, result text, artifacts, decisions, and execution summary.
        """
        # Build execution summary from cost events if available
        execution_summary: dict[str, Any] = {}
        total_cost = 0.0
        for event in self._changes:
            if isinstance(event, TokensConsumed):
                total_cost += event.cost_usd
        if total_cost > 0:
            execution_summary["cost_usd"] = total_cost

        return TaskOutcome(
            task_summary=self.task_description or "",
            result_text=self.result or "",
            artifacts=tuple(self.local_artifacts),
            decisions=tuple(self.local_decisions),
            context_updates={},
            execution_summary=execution_summary,
        )

    @classmethod
    def load_from_history(cls, events: list[DomainEvent]) -> "AgentSession":
        """Reconstruct AgentSession by replaying events."""
        assert events, "Cannot load from empty event history"
        first_event = events[0]
        assert isinstance(first_event, AgentCreated), (
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

    def is_terminal(self) -> bool:
        """True if COMPLETED or FAILED."""
        return self.status in (AgentStatus.COMPLETED, AgentStatus.FAILED)

    def is_leaf(self) -> bool:
        """True if WORKER with no children."""
        return self.role == AgentRole.WORKER and len(self.child_ids) == 0

    def fail_with_reason(self, reason: str) -> None:
        """Mark agent as failed with WorkFailed event."""
        failed_event = WorkFailed(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            reason=reason,
        )
        self._apply(failed_event)
        self._changes.append(failed_event)

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
        self._apply(complexity_event)
        self._changes.append(complexity_event)

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
        self._apply(cost_event)
        self._changes.append(cost_event)

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
        self._apply(limit_event)
        self._changes.append(limit_event)

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
            prompt_type: Type of prompt (complexity_evaluation, task_decomposition, worker_execution).
            target: Where prompt is sent (llm, claude_code, openhands, etc.).
        """
        prompt_event = PromptSent(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            prompt=prompt,
            prompt_type=prompt_type,
            target=target,
        )
        self._apply(prompt_event)
        self._changes.append(prompt_event)

    def apply_subtasks_and_spawn_children(
        self,
        subtasks: list,
        child_role: str,
        spawn_payload: SpawnPayload,
    ) -> list[tuple[UUID, Any]]:
        """Apply subtask decomposition and spawn children (pure domain method).

        Returns list of (child_id, subtask) tuples for the orchestrator to create.
        """
        from core.domain.values.subtask import Subtask

        subtasks_event = SubtasksDefined(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            subtasks=subtasks,
        )
        self._apply(subtasks_event)
        self._changes.append(subtasks_event)

        children_to_spawn: list[tuple[UUID, Subtask]] = []

        for sibling_index, subtask in enumerate(subtasks):
            child_id = uuid4()
            child_event = ChildSpawned(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                child_id=child_id,
                child_role=child_role,
                subtask=subtask,
                child_config=subtask.config,
                parent_context=spawn_payload.model_dump(),
                sibling_index=sibling_index,
            )
            self._apply(child_event)
            self._changes.append(child_event)
            children_to_spawn.append((child_id, subtask))

        # Transition to WAITING status
        status_event = StatusChanged(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            old_status=self.status.value,
            new_status=AgentStatus.WAITING.value,
            reason="Decomposed task, waiting for child agents",
        )
        self._apply(status_event)
        self._changes.append(status_event)

        return children_to_spawn

    def start_worker_execution(self, tool_name: str) -> None:
        """Emit CodeGenerationStarted event (pure domain method)."""
        started_event = CodeGenerationStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            tool_name=tool_name,
        )
        self._apply(started_event)
        self._changes.append(started_event)

    def apply_worker_event(self, tool_event: DomainEvent) -> None:
        """Apply a worker tool event with corrected sequence number."""
        event_data = tool_event.model_dump(exclude={"aggregate_id", "sequence_number"})
        event_data["aggregate_id"] = self.agent_id
        event_data["sequence_number"] = self._next_sequence()
        corrected_event = type(tool_event)(**event_data)
        self._apply(corrected_event)
        self._changes.append(corrected_event)

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
        self._apply(event)
        self._changes.append(event)

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
        self._apply(event)
        self._changes.append(event)

    def emit_operation_started(self, operation_type: str) -> None:
        """Emit OperationStarted event for timing observability."""
        event = OperationStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            operation_type=operation_type,
        )
        self._apply(event)
        self._changes.append(event)

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
        self._apply(event)
        self._changes.append(event)

    # -------------------------------------------------------------------------
    # Run-Level Timing Events (BOSS only)
    # -------------------------------------------------------------------------

    def emit_run_started(
        self,
        task_description: str,
        instance_id: str | None = None,
    ) -> None:
        """Emit RunStarted event when execution run begins.

        Should only be called on BOSS agent (root of hierarchy).
        """
        event = RunStarted(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            task_description=task_description,
            instance_id=instance_id,
        )
        self._apply(event)
        self._changes.append(event)

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
        self._apply(event)
        self._changes.append(event)
