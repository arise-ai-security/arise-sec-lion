"""Domain events for the multi-agent system.

This module defines the base event class and all domain events used in the
event sourcing architecture. All events are immutable (frozen) Pydantic models.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from core.domain.subtask import Subtask


def _utc_now() -> datetime:
    """Get current UTC datetime (Python 3.12+ compatible)."""
    return datetime.now(UTC)


class DomainEvent(BaseModel):
    """Base class for all domain events.

    Domain events represent facts that have occurred in the system. They are
    immutable and used to reconstruct aggregate state via event sourcing.

    Attributes:
        event_id: Unique identifier for this event instance.
        aggregate_id: ID of the aggregate (AgentSession) this event belongs to.
        sequence_number: Order of this event in the aggregate's event stream.
        occurred_at: Timestamp when the event occurred.
        metadata: Additional context about the event (e.g., user_id, correlation_id).
    """

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


# Specific Domain Events


class AgentCreated(DomainEvent):
    """Event raised when a new agent session is created.

    Attributes:
        role: The role of the agent (BOSS, MANAGER, or WORKER).
        parent_id: ID of the parent agent (None for BOSS).
        config: Configuration settings for this agent.
    """

    role: str
    parent_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class TaskAssigned(DomainEvent):
    """Event raised when a task is assigned to an agent.

    Attributes:
        task_description: The task to be executed.
        constraints: Constraints or requirements for task execution.
    """

    task_description: str
    constraints: dict[str, Any] = Field(default_factory=dict)


class StatusChanged(DomainEvent):
    """Event raised when an agent's status changes.

    Attributes:
        old_status: The previous status.
        new_status: The new status.
        reason: Explanation for the status change.
    """

    old_status: str
    new_status: str
    reason: str = ""


class SubtasksDefined(DomainEvent):
    """Event raised when a MANAGER agent decomposes a task into subtasks.

    Attributes:
        subtasks: List of Subtask value objects defining atomic units of work.
    """

    subtasks: list[Subtask]


class ChildSpawned(DomainEvent):
    """Event raised when an agent spawns a child agent.

    This event captures the parent's decision about:
    1. What task to assign (subtask.description)
    2. How the child should operate (child_config)

    The child_config field allows the parent to specify which models/hyperparameters
    the child uses for different operations (complexity evaluation, task decomposition).
    This makes the system "super flexible" - parents control all child behavior.

    Attributes:
        child_id: UUID of the newly created child agent.
        child_role: Role of the child agent (PENDING initially, becomes MANAGER or WORKER).
        subtask: The specific Subtask value object assigned to this child.
        child_config: Serialized AgentConfig dict for the child agent.
                     Specifies models, hyperparameters, and tool selection.
    """

    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]


class WorkCompleted(DomainEvent):
    """Event raised when an agent successfully completes its work.

    Attributes:
        result: The final result or output of the work.
    """

    result: str


class WorkFailed(DomainEvent):
    """Event raised when an agent fails to complete its work.

    Attributes:
        reason: Explanation of why the work failed.
    """

    reason: str


class CodeGenerationStarted(DomainEvent):
    """Event raised when a WORKER agent starts code generation/execution.

    Attributes:
        tool_name: Name of the worker tool being used (e.g., "claude-code", "openhands").
    """

    tool_name: str


class ThoughtCaptured(DomainEvent):
    """Event raised when a WORKER tool emits thinking/logging output.

    This event captures real-time stdout/stderr from worker tools (Claude Code, OpenHands)
    to provide visibility into the agent's reasoning and execution process.

    Attributes:
        content: The captured thought/log content.
        stream: Which stream this came from ("stdout", "stderr", "tool").
    """

    content: str
    stream: str = "tool"


class ChildCompleted(DomainEvent):
    """Event raised when a child agent completes its work.

    This event is used by parent agents (MANAGER, BOSS) to track the completion
    status of their child agents and aggregate results.

    Attributes:
        child_id: UUID of the child agent that completed.
        result: The result produced by the child agent.
    """

    child_id: UUID
    result: str


class ComplexityEvaluated(DomainEvent):
    """Event raised when an agent evaluates its task complexity.

    After receiving a task assignment, child agents evaluate whether their
    task is SIMPLE (can be executed directly) or COMPLEX (requires decomposition).
    This determines whether the agent will act as WORKER or MANAGER.

    Attributes:
        complexity: The evaluated complexity ("simple" or "complex").
        determined_role: The role determined based on complexity (worker or manager).
        reasoning: LLM's reasoning for the complexity decision.
    """

    complexity: str  # "simple" or "complex"
    determined_role: str  # "worker" or "manager"
    reasoning: str = ""


class BudgetAllocated(DomainEvent):
    """Event raised when budget is allocated to an agent.

    Budget is the numeric resource allocation that an agent can use to perform its tasks.
    Initial budget is typically set when the agent is created or when a parent allocates
    resources to a child agent.

    Attributes:
        amount: The budget amount allocated to this agent.
        source: Source of the budget allocation (e.g., "initial", "parent", "reward").
    """

    amount: float
    source: str = "initial"


class BudgetAdjusted(DomainEvent):
    """Event raised when an agent's budget is adjusted.

    Budget can be adjusted based on the success or failure of subordinate agents
    (reward mechanism) or other factors. Positive adjustments increase budget,
    negative adjustments decrease it.

    Attributes:
        adjustment: The amount to adjust (positive for increase, negative for decrease).
        reason: Explanation for the budget adjustment.
        new_balance: The new budget balance after adjustment.
    """

    adjustment: float
    reason: str
    new_balance: float


class TaskEnqueued(DomainEvent):
    """Event raised when a subtask is added to the agent's task queue.

    The task queue is a FIFO queue of subtasks that the agent needs to process.
    Each subtask is either executed directly (atomic task) or delegated to
    subordinate agents (composite task).

    Attributes:
        subtask: The Subtask value object being added to the queue.
    """

    subtask: Subtask


class TaskDequeued(DomainEvent):
    """Event raised when a subtask is removed from the agent's task queue for processing.

    When an agent is ready to work on the next task, it dequeues the first
    subtask from its task queue and begins processing.

    Attributes:
        subtask: The Subtask value object being dequeued.
    """

    subtask: Subtask


# ============================================================================
# Termination Events
# ============================================================================


class TerminationReason(str):
    """Constants for agent termination reasons."""

    BUDGET_DEPLETED = "budget_depleted"
    OBJECTIVE_COMPLETED = "objective_completed"
    SUBTASK_FAILED = "subtask_failed"
    SUBTREE_ABORTED = "subtree_aborted"
    PARENT_ABORTED = "parent_aborted"


class AgentTerminated(DomainEvent):
    """Event raised when an agent node is terminated.

    Agent termination occurs in three main scenarios (per Agent Lifecycle):
    1. Budget Depletion: Agent's budget drops to zero or below
    2. Objective Completion: Agent successfully completes and reports to supervisor
    3. Failed Subtask: Agent fails a critical subtask and supervisor decides not to reassign

    When a subtask is aborted by supervisor, every agent under the supervisor's
    subtree is also terminated (cascading termination).

    Attributes:
        reason: The termination reason (budget_depleted, objective_completed,
                subtask_failed, subtree_aborted, parent_aborted).
        detail: Additional human-readable details about the termination.
        final_budget: The agent's budget at the time of termination.
        cascade: Whether this termination should cascade to child agents.
    """

    reason: str
    detail: str = ""
    final_budget: float = 0.0
    cascade: bool = False


class SubtreeAborted(DomainEvent):
    """Event raised when a supervisor decides to abort an entire subtree.

    When a supervisor determines that a subtask cannot be completed (due to
    budget constraints, task importance, or repeated failures), it aborts
    the subtask. This triggers termination of all agents in that subtree.

    Attributes:
        subtask_id: The ID of the subtask being aborted.
        child_ids_to_terminate: List of child agent IDs that should be terminated.
        reason: Why the subtree is being aborted.
    """

    subtask_id: UUID | None = None
    child_ids_to_terminate: list[UUID] = []
    reason: str = ""


# ============================================================================
# Budget Recollection Events (Reward/Penalty Mechanism)
# ============================================================================


class BudgetRecollected(DomainEvent):
    """Event raised when a supervisor recollects budget from a completed child.

    When a child agent completes (success or failure), the supervisor recollects
    the remaining budget with a reward or penalty ratio applied:
    - Success: remaining_budget * reward_ratio (e.g., 1.2x)
    - Failure: remaining_budget * penalty_ratio (e.g., 0.0 or 1.0)

    Example from description:
    - Subordinate 2 succeeds with 111 budget remaining
    - Reward ratio = 1.2x
    - Supervisor receives: 111 * 1.2 = 133.2

    Attributes:
        child_id: The ID of the child agent whose budget is being recollected.
        original_allocation: The budget originally allocated to the child.
        remaining_budget: The child's remaining budget at completion.
        ratio_applied: The reward/penalty ratio applied (1.2 for success, etc.).
        amount_recollected: The actual amount recollected (remaining * ratio).
        child_succeeded: Whether the child completed successfully.
    """

    child_id: UUID
    original_allocation: float
    remaining_budget: float
    ratio_applied: float
    amount_recollected: float
    child_succeeded: bool


class ChildFailed(DomainEvent):
    """Event raised when a child agent fails its assigned task.

    This event is distinct from ChildCompleted and is used for tracking
    failure analytics and determining penalty ratios.

    Attributes:
        child_id: UUID of the child agent that failed.
        failure_reason: The reason for the failure.
        budget_at_failure: The child's remaining budget when it failed.
        retry_attempted: Whether a retry was attempted before recording failure.
    """

    child_id: UUID
    failure_reason: str
    budget_at_failure: float = 0.0
    retry_attempted: bool = False


class AllChildrenFailed(DomainEvent):
    """Event raised when all sibling subordinate nodes fail a task.

    Per the description, if all subordinate nodes fail, the supervisor creates
    a penalty on all of them. This event signals that condition.

    Attributes:
        subtask_description: Description of the failed subtask.
        child_ids: List of child agent IDs that all failed.
        penalty_ratio: The penalty ratio to apply to budget recollection.
    """

    subtask_description: str
    child_ids: list[UUID]
    penalty_ratio: float = 0.0


# ============================================================================
# Verification Events (Agentic Verification Task Strategy)
# ============================================================================


class VerificationInjected(DomainEvent):
    """Event raised when a verification task is injected into the queue.

    The supervisor injects verification tasks based on heuristics:
    1. Complexity of Previous Task: if subtree is large and task is complex
    2. Random Probability: small probability for random verification
    3. Suspicious Reports: if reported edits don't match task complexity
    4. Time Since Last Verification: if too long since last verification
    5. Budget Availability: if sufficient budget exists

    Attributes:
        target_subtask: The subtask being verified (just completed).
        target_child_id: The child that completed the subtask being verified.
        injection_reason: Why this verification was injected.
        estimated_verification_cost: Expected budget cost for verification.
    """

    target_subtask: Subtask
    target_child_id: UUID
    injection_reason: str
    estimated_verification_cost: float = 0.0


class VerifierSpawned(DomainEvent):
    """Event raised when an independent verifier sub-agent is spawned.

    The verifier agent is expected to be different from all original
    subordinate nodes (often a more advanced model with deep-thinking
    capabilities like claude-sonnet-4-5).

    Attributes:
        verifier_id: UUID of the spawned verifier agent.
        target_subtask: The subtask being verified.
        target_child_id: The child whose work is being verified.
        verifier_config: Configuration for the verifier agent.
    """

    verifier_id: UUID
    target_subtask: Subtask
    target_child_id: UUID
    verifier_config: dict[str, Any] = {}


class VerificationCompleted(DomainEvent):
    """Event raised when a verifier agent completes its verification.

    Attributes:
        verifier_id: UUID of the verifier agent.
        target_subtask: The subtask that was verified.
        target_child_id: The child whose work was verified.
        verification_passed: Whether the original work passed verification.
        verification_report: Detailed report from the verifier.
        issues_found: List of issues found during verification.
    """

    verifier_id: UUID
    target_subtask: Subtask
    target_child_id: UUID
    verification_passed: bool
    verification_report: str = ""
    issues_found: list[str] = []


class TaskReinjected(DomainEvent):
    """Event raised when a task is re-injected after failed verification.

    If the verifier reports that the previous task was not correctly finished,
    the supervisor re-injects the same task to the head of the queue and
    respawns subordinate nodes to redo the task with additional context.

    Attributes:
        subtask: The subtask being re-injected for redo.
        verification_context: Context from the verifier to help redo.
        original_child_id: The child that originally failed this task.
        retry_count: Number of times this task has been reinjected.
    """

    subtask: Subtask
    verification_context: str
    original_child_id: UUID
    retry_count: int = 1


# ============================================================================
# Heuristic Events (For Verification Decisions)
# ============================================================================


class VerificationHeuristicEvaluated(DomainEvent):
    """Event raised when verification heuristics are evaluated.

    This event captures the supervisor's decision-making process for whether
    to inject a verification task. It provides an audit trail for debugging
    and tuning the heuristics.

    Attributes:
        subtask_completed: The subtask that just completed.
        child_id: The child that completed the subtask.
        complexity_score: Estimated complexity of the subtask (0.0-1.0).
        subtree_size: Number of agents in the subtree.
        random_roll: Random value used for probability check (0.0-1.0).
        random_threshold: Threshold for random verification injection.
        time_since_last_verification: Seconds since last verification task.
        edits_count: Number of edits reported by the child.
        expected_edits_range: Expected range of edits for task complexity.
        suspicious: Whether the completion is flagged as suspicious.
        available_budget: Budget available for verification.
        verification_decided: Whether a verification task was injected.
        decision_reasoning: Human-readable explanation of the decision.
    """

    subtask_completed: Subtask
    child_id: UUID
    complexity_score: float = 0.0
    subtree_size: int = 0
    random_roll: float = 0.0
    random_threshold: float = 0.1
    time_since_last_verification: float = 0.0
    edits_count: int = 0
    expected_edits_range: tuple[int, int] = (0, 0)
    suspicious: bool = False
    available_budget: float = 0.0
    verification_decided: bool = False
    decision_reasoning: str = ""


# ============================================================================
# Multi-Model Strategy Events (Different Models for Subordinates)
# ============================================================================


class SubordinatesSpawned(DomainEvent):
    """Event raised when multiple subordinate nodes are spawned for same subtask.

    Default Task Spawning Behavior:
    When a supervisor decides to create a sub-task, it spawns by default 3 agents
    with different internal LLM models with the same assigned task and equal
    budget split. This diversity facilitates the possibility of success because
    different models have different strengths and weaknesses.

    The supervisor only needs ONE successful subordinate to complete the sub-task,
    and the rest are immediately terminated to save budget.

    Budget Recollection:
    - Success: Supervisor recollects remaining budget with reward ratio (1.2x)
    - Terminated: Lose given budget with no reward or penalty (1.0x)
    - Failed: Return remaining budget with penalty ratio (0.8x)

    Example from description:
    - Subtask: "Open the text file"
    - Supervisor spawns 3 subordinates with: Claude Sonnet, Gemini Pro, GPT-4o
    - Each may use different method: vim, vi, nano
    - Total budget for subtask: 333, each subordinate gets 111

    Attributes:
        subtask: The subtask assigned to all subordinates.
        total_budget_allocated: Total budget allocated to this subtask.
        subordinate_configs: List of subordinate configurations, each containing:
            - child_id: UUID of the subordinate
            - config: AgentConfig dict for the subordinate
            - budget: Budget allocated to this subordinate
            - model: The LLM model for this subordinate
            - method_hint: Optional hint about the approach to use
    """

    subtask: Subtask
    total_budget_allocated: float = 0.0
    subordinate_configs: list[dict[str, Any]] = []


class FirstSuccessRecorded(DomainEvent):
    """Event raised when the first subordinate succeeds at a parallel task.

    When multiple subordinates work on the same subtask in parallel, the first
    to succeed triggers:
    1. Recording of the winning result
    2. Immediate termination of all sibling subordinates to save budget
    3. Budget recollection with reward ratio for winner

    Attributes:
        winning_child_id: The child that succeeded first.
        subtask: The subtask that was completed.
        sibling_ids_terminated: List of sibling IDs that were terminated.
        method_used: The method/approach used by the winning child.
        result: The result produced by the winning child.
        budget_recollected_from_winner: Budget recollected from winner (with reward ratio).
        budget_recollected_from_siblings: Total budget recollected from terminated siblings.
    """

    winning_child_id: UUID
    subtask: Subtask
    sibling_ids_terminated: list[UUID] = []
    method_used: str = ""
    result: str = ""
    budget_recollected_from_winner: float = 0.0
    budget_recollected_from_siblings: float = 0.0


class AllSubordinatesFailed(DomainEvent):
    """Event raised when all subordinate nodes for a subtask have failed.

    This is a terminal condition for the subtask. The supervisor must decide
    whether to:
    1. Retry the subtask with revised approach (re-insert to queue)
    2. Abort the subtask and report failure to its own supervisor

    Attributes:
        subtask: The subtask that all subordinates failed to complete.
        failed_child_ids: List of all subordinates that failed.
        failure_reasons: Map of child_id -> failure reason.
        total_budget_lost: Total budget consumed by all failed subordinates.
    """

    subtask: Subtask
    failed_child_ids: list[UUID] = []
    failure_reasons: dict[str, str] = {}
    total_budget_lost: float = 0.0


class SubtaskRetried(DomainEvent):
    """Event raised when a failed subtask is being retried.

    After verification fails or all subordinates fail, the supervisor can
    re-insert a REVISED subtask to the head of the queue.

    Attributes:
        original_subtask: The original subtask that failed.
        revised_subtask: The revised subtask with additional context.
        retry_count: Number of times this subtask has been retried.
        revision_reason: Why the subtask was revised.
        additional_context: Context from failures/verification to help retry.
    """

    original_subtask: Subtask
    revised_subtask: Subtask
    retry_count: int = 1
    revision_reason: str = ""
    additional_context: str = ""
