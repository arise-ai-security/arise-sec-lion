"""Domain events for the multi-agent system (event sourcing)."""

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from core.domain.values.json_types import JsonObject
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
    briefing contains parent context for domain-specific prompt strategies.
    """

    role: str
    parent_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    sibling_index: int = 0
    briefing: dict[str, Any] | None = None
    depends_on: list[int] = Field(default_factory=list)
    success_criteria: str = ""  # From Subtask.success_criteria, used by verification

    # Structured child scoping — from Subtask, persisted for replay
    target_paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    search_hints: list[str] = Field(default_factory=list)


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
    """Parent spawned a child agent with Briefing context.

    sibling_index tracks the child's position among siblings (0-indexed)
    for left-to-right execution ordering.
    """

    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]
    briefing: dict[str, Any] = Field(default_factory=dict)  # Serialized Briefing
    sibling_index: int = 0  # Position among siblings (0 = first/leftmost)


class WorkCompleted(DomainEvent):
    """Agent completed work successfully."""

    result: str


class WorkFailed(DomainEvent):
    """Agent failed to complete work."""

    reason: str


class VerificationFailed(DomainEvent):
    """Worker output failed verification checks.

    Emitted when a completed worker's output doesn't pass quality gates.
    Transitions agent from COMPLETED to FAILED.
    """

    failed_stage: str  # "structural", "deterministic", "execution", "judge"
    feedback: str  # Structured feedback for retry/parent
    stages_passed: list[str] = Field(default_factory=list)  # Stages that passed before failure
    score: int = 0  # Judge satisfaction score (0-100), 0 for non-judge stages


class VerificationPassed(DomainEvent):
    """Worker output passed all verification checks.

    Observability event — does not change agent state (already COMPLETED).
    """

    feedback: str = ""  # Judge feedback when passed
    score: int = 100  # Judge satisfaction score (0-100)


class DecisionInfeasible(DomainEvent):
    """Agent determined task is infeasible within given constraints.

    Emitted when LLM responds with constraints_unsatisfiable.
    Transitions agent to FAILED with structured infeasibility feedback
    that can trigger parent re-decomposition.
    """

    reason: str
    minimum_subtasks: int | None = None
    minimum_depth: int | None = None


class RedecompositionTriggered(DomainEvent):
    """Parent re-decomposes after child signals infeasible.

    Transitions parent from WAITING → ANALYZING for a new decomposition
    attempt with adjusted constraints or strategy.
    """

    trigger_child_id: UUID
    reason: str


class ProbeStarted(DomainEvent):
    """Agent started a read-only probe before making a decision.

    Observability event — does not change agent state.
    """

    probe_type: str  # "file_check", "repo_scan", "api_query", etc.


class ProbeCompleted(DomainEvent):
    """Agent completed a read-only probe.

    Observability event — does not change agent state.
    """

    probe_type: str
    result_summary: str = ""


class RetryScheduled(DomainEvent):
    """Agent scheduled for retry after failure.

    Transitions agent from FAILED back to ANALYZING for re-execution.
    Tracks retry attempt number and optional model escalation.
    """

    attempt: int  # 1-indexed retry attempt number
    reason: str  # Why retry was scheduled (original failure reason)
    escalated_model: str | None = None  # New model if escalated, None if same
    is_verification_retry: bool = False


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
    """Child agent completed, parent notified with structured Report."""

    child_id: UUID
    result: str  # Simple result text (kept for quick access)
    report: dict[str, Any] = Field(default_factory=dict)  # Serialized Report


class ChildFailed(DomainEvent):
    """Child agent failed, parent notified with error details.

    Triggers failure propagation up the hierarchy.
    """

    child_id: UUID
    reason: str  # Error message from child


class ComplexityEvaluated(DomainEvent):
    """Task complexity evaluated, role determined (worker/manager)."""

    complexity: str
    determined_role: str
    reasoning: str = ""


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
    operation: str  # "complexity_evaluation", "task_decomposition", "worker_execution", "verification", "context_condense"


class WorkerCostItem(BaseModel):
    """Individual worker-side cost record captured from an SDK."""

    model: str = ""
    cost_usd: float = 0.0
    timestamp: float | None = None


class WorkerResponseLatencyItem(BaseModel):
    """Per-response latency captured by a worker SDK."""

    model: str = ""
    latency_seconds: float = 0.0
    response_id: str = ""


class WorkerTokenUsageItem(BaseModel):
    """Per-response token usage captured by a worker SDK."""

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    context_window: int = 0
    per_turn_token: int = 0
    response_id: str = ""


class WorkerUsageMetrics(BaseModel):
    """Aggregated and per-call worker LLM metrics for one SDK usage ID."""

    usage_id: str
    model: str | None = None
    accumulated_cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_items: list[WorkerCostItem] = Field(default_factory=list)
    response_latencies: list[WorkerResponseLatencyItem] = Field(default_factory=list)
    token_usages: list[WorkerTokenUsageItem] = Field(default_factory=list)


class WorkerCostRecorded(DomainEvent):
    """Worker tool execution incurred cost.

    Tracks costs from Claude Code, OpenHands, or other worker tools.
    Some tools may not provide token counts (e.g., PTY-based execution).
    """

    tool_name: str  # "claude_code", "openhands"
    model: str | None = None  # Underlying model if known
    tokens: int | None = None  # Total tokens if available
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_metrics: list[WorkerUsageMetrics] = Field(default_factory=list)
    cost_usd: float = 0.0
    duration_seconds: float = 0.0

    @property
    def total_recorded_tokens(self) -> int | None:
        """Return the best available total token count for this worker event."""
        if self.tokens is not None:
            return self.tokens

        token_parts = (
            self.prompt_tokens,
            self.completion_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.reasoning_tokens,
        )
        if not any(part is not None for part in token_parts):
            return None

        return sum(part or 0 for part in token_parts)

    @property
    def model_costs(self) -> dict[str, float]:
        """Return the most accurate per-model cost breakdown available."""
        if self.usage_metrics:
            costs: dict[str, float] = {}
            for usage in self.usage_metrics:
                if not usage.model:
                    continue
                costs[usage.model] = costs.get(usage.model, 0.0) + usage.accumulated_cost_usd
            if costs:
                return costs

        if self.model:
            return {self.model: self.cost_usd}
        return {}


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

    role: str  # "boss", "manager", "worker"
    depth: int


class AgentExecutionFinished(DomainEvent):
    """Emitted when agent completes or fails execution.

    Contains duration calculated from paired AgentExecutionStarted event.
    Enables answering: "Which worker took longest?", "Total time by role"
    """

    role: str  # "boss", "manager", "worker"
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
    domain_metadata: JsonObject | None = None


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
    """Shared store created for a run.

    One shared store per execution hierarchy (keyed by root_id).
    """

    root_id: UUID
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
