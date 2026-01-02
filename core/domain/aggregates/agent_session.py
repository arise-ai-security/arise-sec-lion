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
from core.domain.values.worker_report import WorkerReport
from core.domain.events.events import (
    AgentCreated,
    ChildCompleted,
    ChildSpawned,
    CodeGenerationStarted,
    ComplexityBudgetAllocated,
    ComplexityBudgetRecollected,
    ComplexityEvaluated,
    DomainEvent,
    LimitEnforced,
    PromptSent,
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
    ) -> "AgentSession":
        instance = cls(agent_id)
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=instance._next_sequence(),
            role=role.value,
            parent_id=parent_id,
            config=config,
            sibling_index=sibling_index,
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
        success: bool = True,
        budget_success_reward_ratio: float = 1.0,
        budget_failure_penalty_ratio: float = 0.0,
    ) -> None:
        """For BOSS/MANAGER: record child completion, complete self when all children done.

        Args:
            child_id: ID of completed child
            result: Simple result text for the event
            task_outcome: Structured TaskOutcome (optional, provides additional context)
            success: Whether child completed successfully (for budget recollection)
            budget_success_reward_ratio: Multiplier for successful completion (Design Choice 3)
            budget_failure_penalty_ratio: Multiplier for failed completion (Design Choice 3)
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

        # Recollect budget from child if applicable (Design Choice 3)
        if child_id in self.child_budget_allocations and task_outcome.complexity_budget_remaining > 0:
            self.recollect_child_budget(
                child_id=child_id,
                child_remaining_budget=task_outcome.complexity_budget_remaining,
                success=success,
                success_reward_ratio=budget_success_reward_ratio,
                failure_penalty_ratio=budget_failure_penalty_ratio,
            )

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
    def _(self, event: ComplexityBudgetAllocated) -> None:
        # Complexity budget allocation (Design Choice 2: Budgeted Tree)
        self.complexity_budget = event.amount
        if event.source == "initial":
            self.initial_complexity_budget = event.amount
        self.version += 1

    @_apply.register
    def _(self, event: ComplexityBudgetRecollected) -> None:
        # Budget recollection from child (Design Choice 3: Budgeted Plus Tree)
        self.complexity_budget += event.amount_recollected
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
        # Complexity budget for tree growth control (Design Choices 2-3)
        self.complexity_budget: float = 0.0
        self.initial_complexity_budget: float = 0.0
        # Track budget allocated to each child for recollection (Design Choice 3)
        self.child_budget_allocations: dict[UUID, float] = {}
        # Worker report for submission to parent (Design Choice 5)
        self.pending_worker_report: WorkerReport | None = None

    def set_hierarchy_limits(self, limits: HierarchyLimits) -> None:
        """Set hierarchy limits for limit enforcement."""
        self.hierarchy_limits = limits

    def set_spawn_payload(self, payload: SpawnPayload) -> None:
        """Set spawn payload received from spawning parent."""
        self.spawn_payload = payload

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

    def allocate_complexity_budget(self, amount: float, source: str = "initial") -> None:
        """Allocate complexity budget to this agent (Design Choice 2: Budgeted Tree).

        Args:
            amount: Budget amount to allocate
            source: "initial" for BOSS, "parent" for children
        """
        budget_event = ComplexityBudgetAllocated(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            amount=amount,
            source=source,
        )
        self._apply(budget_event)
        self._changes.append(budget_event)

    def is_below_budget_threshold(self, initial_boss_budget: float, threshold_ratio: float) -> bool:
        """Check if complexity budget is below threshold (Design Choice 2).

        Args:
            initial_boss_budget: The initial budget given to BOSS agent
            threshold_ratio: Ratio of initial budget below which agent becomes WORKER

        Returns:
            True if budget is below threshold, False otherwise
        """
        if self.complexity_budget <= 0:
            return False  # No budget allocated, use structural limits
        threshold = initial_boss_budget * threshold_ratio
        return self.complexity_budget < threshold

    def calculate_child_budgets(
        self,
        subtasks: list,
    ) -> list[float]:
        """Calculate proportional budget allocation for children (Design Choice 3).

        Budget is distributed proportionally based on subtask.budget_weight.
        child_budget = parent_budget * (weight / total_weights)

        Args:
            subtasks: List of Subtask objects with budget_weight field

        Returns:
            List of budget amounts for each child (same order as subtasks)
        """
        if self.complexity_budget <= 0 or not subtasks:
            return [0.0] * len(subtasks)

        # Calculate total weight
        total_weight = sum(s.budget_weight for s in subtasks)
        if total_weight <= 0:
            # Equal distribution if all weights are 0
            return [self.complexity_budget / len(subtasks)] * len(subtasks)

        # Proportional allocation
        return [
            self.complexity_budget * (s.budget_weight / total_weight)
            for s in subtasks
        ]

    def record_child_budget_allocation(self, child_id: UUID, amount: float) -> None:
        """Record budget allocated to a child for later recollection (Design Choice 3).

        Args:
            child_id: ID of the child agent
            amount: Budget amount allocated
        """
        self.child_budget_allocations[child_id] = amount

    def recollect_child_budget(
        self,
        child_id: UUID,
        child_remaining_budget: float,
        success: bool,
        success_reward_ratio: float,
        failure_penalty_ratio: float,
    ) -> None:
        """Recollect budget from completed child (Design Choice 3).

        Parent reclaims budget with reward/penalty ratio based on success:
        - Success: original_allocation * success_reward_ratio
        - Failure: original_allocation * failure_penalty_ratio

        Args:
            child_id: ID of the completed child
            child_remaining_budget: Child's remaining unused budget
            success: Whether child completed successfully
            success_reward_ratio: Multiplier for successful completion (e.g., 1.2)
            failure_penalty_ratio: Multiplier for failed completion (e.g., 0.0)
        """
        original_allocation = self.child_budget_allocations.get(child_id, 0.0)
        if original_allocation <= 0:
            return  # No budget was allocated

        ratio = success_reward_ratio if success else failure_penalty_ratio
        amount_recollected = child_remaining_budget * ratio

        recollect_event = ComplexityBudgetRecollected(
            aggregate_id=self.agent_id,
            sequence_number=self._next_sequence(),
            child_id=child_id,
            original_allocation=original_allocation,
            remaining=child_remaining_budget,
            ratio_applied=ratio,
            amount_recollected=amount_recollected,
        )
        self._apply(recollect_event)
        self._changes.append(recollect_event)

    def build_task_outcome(self) -> TaskOutcome:
        """Build structured task outcome to return to parent.

        Includes task summary, result text, artifacts, decisions, execution summary,
        and remaining complexity budget for recollection (Design Choice 3).
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
            complexity_budget_remaining=self.complexity_budget,  # Design Choice 3
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
        child_budgets: list[float] | None = None,
    ) -> list[tuple[UUID, Any]]:
        """Apply subtask decomposition and spawn children (pure domain method).

        Args:
            subtasks: List of Subtask objects for decomposition
            child_role: Role for child agents
            spawn_payload: Base spawn payload (will be customized per-child with budget)
            child_budgets: Optional list of budget amounts for each child (Design Choice 3)

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

            # Create per-child spawn payload with specific budget (Design Choice 3)
            child_budget = 0.0
            if child_budgets is not None and sibling_index < len(child_budgets):
                child_budget = child_budgets[sibling_index]
                # Record allocation for later recollection
                self.record_child_budget_allocation(child_id, child_budget)

            # Calculate total weights for budget context (Design Choice 4)
            total_weights = sum(s.budget_weight for s in subtasks)

            # Extract justification as dict for serialization (Design Choice 4)
            justification_dict = None
            if subtask.justification:
                justification_dict = subtask.justification.model_dump()

            child_payload = SpawnPayload(
                parent_task=spawn_payload.parent_task,
                parent_role=spawn_payload.parent_role,
                depth=spawn_payload.depth,
                ancestry=spawn_payload.ancestry,
                decisions=spawn_payload.decisions,
                constraints=spawn_payload.constraints,
                execution_limits=spawn_payload.execution_limits,
                complexity_budget=child_budget,
                # Design Choice 4: Thinker Justification Context
                subtask_justification=justification_dict,
                budget_weight=subtask.budget_weight,
                total_weights=total_weights,
                num_siblings=len(subtasks),
            )

            child_event = ChildSpawned(
                aggregate_id=self.agent_id,
                sequence_number=self._next_sequence(),
                child_id=child_id,
                child_role=child_role,
                subtask=subtask,
                child_config=subtask.config,
                parent_context=child_payload.model_dump(),
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
