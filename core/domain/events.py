"""Domain events for the multi-agent system (event sourcing)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from core.domain.subtask import Subtask


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DomainEvent(BaseModel):
    """Base class for immutable domain events."""

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentCreated(DomainEvent):
    """Agent session created."""

    role: str
    parent_id: UUID | None = None
    config: dict[str, Any] = Field(default_factory=dict)


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
    """Parent spawned a child agent with config."""

    child_id: UUID
    child_role: str
    subtask: Subtask
    child_config: dict[str, Any]


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


class ChildCompleted(DomainEvent):
    """Child agent completed, parent notified."""

    child_id: UUID
    result: str


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
