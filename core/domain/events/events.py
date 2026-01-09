"""Domain events for the multi-agent system (event sourcing)."""

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from core.domain.values.subtask import Subtask


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _deep_copy_value(v: Any) -> Any:
    """Deep copy dicts and lists to prevent external mutation."""
    if isinstance(v, dict):
        return copy.deepcopy(v)
    if isinstance(v, list):
        return copy.deepcopy(v)
    return v


class DomainEvent(BaseModel):
    """Base class for immutable domain events.

    All dict and list fields are deep copied on construction to ensure true
    immutability. The frozen=True config prevents attribute reassignment, but
    mutable containers could still be mutated externally without the deep copy.

    The model_validator automatically deep-copies all dict/list values in input
    data, eliminating the need for per-field validators in subclasses.
    """

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _deep_copy_mutable_fields(cls, data: Any) -> Any:
        """Deep copy all dict/list values to ensure immutability.

        This eliminates duplicate field validators across all event subclasses.
        Runs before field validation, deep-copying any dict or list values.
        """
        if isinstance(data, dict):
            return {k: _deep_copy_value(v) for k, v in data.items()}
        return data


class AgentCreated(DomainEvent):
    """Agent session created.

    sibling_index tracks position among siblings for left-to-right ordering.
    Root agents (BOSS) have sibling_index=0.
    spawn_payload contains parent context for branch detection in SEC-bench.
    """

    role: str
    parent_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    sibling_index: int = 0  # Position among siblings (0 = first/leftmost)
    spawn_payload: dict[str, Any] | None = None  # Parent context for child agents


class TaskAssigned(DomainEvent):
    """Task assigned to agent."""

    task_description: str
    constraints: dict[str, Any] = Field(default_factory=dict)


class StatusChanged(DomainEvent):
    """Agent status transition."""

    old_status: str
    new_status: str
    reason: str = ""


class SubtasksDefined(DomainEvent):
    """MANAGER decomposed task into subtasks."""

    subtasks: list[Subtask]


class ChildSpawned(DomainEvent):
    """Parent spawned a child agent with rich context.

    Includes SpawnPayload for bidirectional context flow.
    sibling_index tracks the child's position among siblings (0-indexed)
    for left-to-right execution ordering.
    """

    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]
    parent_context: dict[str, Any] = Field(default_factory=dict)  # Serialized SpawnPayload
    sibling_index: int = 0  # Position among siblings (0 = first/leftmost)


class WorkCompleted(DomainEvent):
    """Agent completed work successfully."""

    result: str


class WorkFailed(DomainEvent):
    """Agent failed to complete work."""

    reason: str


class CodeGenerationStarted(DomainEvent):
    """WORKER started execution with tool."""

    tool_name: str


class ThoughtCaptured(DomainEvent):
    """Worker tool output captured (thinking, progress, output, debug)."""

    content: str
    stream: str = "tool"
    output_type: str = "output"


class PromptSent(DomainEvent):
    """Prompt sent to LLM or worker tool for processing.

    Captures the full prompt for observability and debugging.
    """

    prompt: str
    prompt_type: str  # "complexity_evaluation", "task_decomposition", "worker_execution"
    target: str  # "llm" or tool name like "claude_code", "openhands"


class ChildCompleted(DomainEvent):
    """Child agent completed, parent notified with structured result.

    Includes TaskOutcome for rich feedback from child to parent.
    """

    child_id: UUID
    result: str  # Simple result text
    child_result: dict[str, Any] = Field(default_factory=dict)  # Rich TaskOutcome structure


class ChildFailed(DomainEvent):
    """Child agent failed, parent notified with error details.

    Triggers failure propagation up the hierarchy.
    """

    child_id: UUID
    reason: str  # Error message from child


class ComplexityEvaluated(DomainEvent):
    """Task complexity evaluated, role determined (worker/manager/researcher)."""

    complexity: str
    determined_role: str
    reasoning: str = ""
    needs_research: bool = False  # True if RESEARCHER role needed before MANAGER


# =============================================================================
# Research Events (RESEARCHER role lifecycle)
# =============================================================================


class ResearchStarted(DomainEvent):
    """RESEARCHER agent started read-only research phase.

    Emitted when a RESEARCHER begins gathering context via tool calling.
    """

    tools_available: list[str] = Field(default_factory=list)  # e.g., ["file_read", "grep_search"]


class ResearchToolCalled(DomainEvent):
    """RESEARCHER agent executed a tool during research.

    Emitted after each tool call to provide real-time progress visibility.
    """

    tool_name: str  # e.g., "file_read", "grep_search"
    tool_arguments: dict[str, Any] = Field(default_factory=dict)  # Arguments passed to tool
    result_preview: str = ""  # Truncated result for display (max ~200 chars)
    iteration: int = 0  # Tool call iteration number (1-indexed)


class ResearchCompleted(DomainEvent):
    """RESEARCHER completed research, ready for role transition.

    Contains findings that will inform task decomposition.
    """

    findings: str  # Summary of research results
    tool_calls_count: int  # Number of tool calls made
    gathered_context: dict[str, Any] = Field(default_factory=dict)  # Structured context


class RoleTransitioned(DomainEvent):
    """Agent transitioned between roles (RESEARCHER -> MANAGER).

    Used when an agent changes role mid-lifecycle after completing a phase.
    """

    from_role: str  # e.g., "researcher"
    to_role: str  # e.g., "manager"
    reason: str = ""  # e.g., "Research phase completed"


# =============================================================================
# Cost Tracking Events
# =============================================================================


class TokensConsumed(DomainEvent):
    """LLM call consumed tokens with associated cost.

    Emitted after each LLM call to track token usage and costs.
    Enables cost aggregation per agent, per operation, and system-wide.
    """

    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    operation: str  # "complexity_evaluation", "task_decomposition", "worker_execution"


class WorkerCostRecorded(DomainEvent):
    """Worker tool execution incurred cost.

    Tracks costs from Claude Code, OpenHands, or other worker tools.
    Some tools may not provide token counts (e.g., PTY-based execution).
    """

    tool_name: str  # "claude_code", "openhands"
    model: str | None = None  # Underlying model if known
    tokens: int | None = None  # Total tokens if available
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


class LimitEnforced(DomainEvent):
    """A system limit was enforced, modifying agent behavior.

    Emitted when limits like max_depth or max_children prevent normal
    operation. The agent continues but with constrained behavior
    (e.g., forcing WORKER role at max depth instead of spawning more managers).
    """

    limit_type: str  # "depth", "children", "agents"
    limit_value: int | float
    attempted_value: int | float
    action_taken: str  # "forced_worker_role", "rejected_children"


# =============================================================================
# Timing/Observability Events
# =============================================================================


class AgentExecutionStarted(DomainEvent):
    """Emitted when agent begins actual work (after PENDING state).

    Used to measure agent execution duration and enable layer-level observability.
    Paired with AgentExecutionFinished for duration calculation.
    """

    role: str  # "boss", "manager", "worker", "researcher"
    depth: int


class AgentExecutionFinished(DomainEvent):
    """Emitted when agent completes or fails execution.

    Contains duration calculated from paired AgentExecutionStarted event.
    Enables answering: "Which worker took longest?", "Total time by role"
    """

    role: str  # "boss", "manager", "worker", "researcher"
    status: str  # "completed", "failed"
    duration_seconds: float


class OperationStarted(DomainEvent):
    """Emitted when an LLM operation begins.

    Used to measure individual operation duration (complexity evaluation,
    task decomposition, worker execution).
    """

    operation_type: str  # "complexity_evaluation", "task_decomposition", "worker_execution"


class OperationFinished(DomainEvent):
    """Emitted when an LLM operation completes.

    Contains duration calculated from paired OperationStarted event.
    Enables answering: "How long did complexity evaluation take?"
    """

    operation_type: str  # "complexity_evaluation", "task_decomposition", "worker_execution"
    duration_seconds: float


class RunStarted(DomainEvent):
    """Emitted when a new execution run begins.

    Stored against root agent (BOSS) aggregate_id.
    Paired with RunCompleted for total run duration calculation.
    """

    task_description: str
    instance_id: str | None = None  # SEC-bench CVE instance ID if applicable


class RunCompleted(DomainEvent):
    """Emitted when an execution run finishes (all agents terminal).

    Contains total duration from RunStarted to completion.
    Stored against root agent (BOSS) aggregate_id.
    """

    status: str  # "completed", "failed"
    duration_seconds: float
    total_agents: int
    completed_agents: int
    failed_agents: int


# =============================================================================
# Shared Context Events
# =============================================================================


class SharedContextCreated(DomainEvent):
    """Shared execution context created for a run.

    One shared context per execution hierarchy (keyed by root_id).
    Stores initial budget and configuration.
    """

    root_id: UUID
    initial_budget_usd: float = 0.0
    config: dict[str, Any] = Field(default_factory=dict)


class ArtifactStored(DomainEvent):
    """Artifact stored in shared context.

    Artifacts are outputs that can be shared across agents:
    - Code files, documentation, analysis results
    - Small content stored inline, large content by hash reference
    """

    key: str  # e.g., "outputs/analysis.json"
    content_type: str  # MIME type
    content: str | None = None  # Small content inline
    content_hash: str | None = None  # SHA256 for large content
    stored_by: UUID  # Agent that stored the artifact
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionRecorded(DomainEvent):
    """Key decision recorded in shared context.

    Decisions are architectural/design choices that should be
    consistent across the agent hierarchy.
    """

    decision_key: str  # e.g., "architecture.database"
    decision_value: str  # JSON-serialized value
    rationale: str
    decided_by: UUID  # Agent that made the decision


class ProgressUpdated(DomainEvent):
    """Progress checkpoint updated in shared context.

    Enables agents to report progress on named checkpoints.
    """

    checkpoint_key: str
    status: str  # "started", "in_progress", "completed", "blocked"
    progress_pct: float = 0.0
    message: str = ""
    reported_by: UUID  # Agent reporting progress


class ConfigOverrideSet(DomainEvent):
    """Configuration override set in shared context.

    Allows runtime configuration changes that propagate
    to all or a subtree of agents.
    """

    config_key: str
    config_value: str  # JSON-serialized value
    set_by: UUID  # Agent that set the override
    scope: str = "global"  # "global" | "subtree:{agent_id}"


# =============================================================================
# Budget Events (part of shared context)
# =============================================================================


class BudgetConsumed(DomainEvent):
    """Budget consumption recorded in shared context.

    Event-sourced budget tracking with OCC guarantees.
    Emitted for each cost-incurring operation.
    """

    consumed_by: UUID  # Agent that consumed budget
    amount_usd: float  # Cost of this operation
    operation: str  # "llm_call", "worker_execution"
    model: str | None = None  # Model used if applicable
    tokens: dict[str, int] = Field(default_factory=dict)  # {"prompt": N, "completion": M}


class BudgetExceeded(DomainEvent):
    """Budget limit exceeded, execution should halt.

    Emitted when consumed budget reaches or exceeds the limit.
    Agents should check for this and stop processing.
    """

    limit_usd: float
    consumed_usd: float
    triggered_by: UUID  # Agent that triggered the limit
