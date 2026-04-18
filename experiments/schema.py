"""Normalized event schema shared by the tree arm and the flat CLI harness.

Produced by:
- Tree arm: post-processing of the event store events into this format
- Flat arm: direct emission from the flat_cli_harness stream parser

Consumed by: Pillar B analysis scripts (load dataset -> compute metrics).

Schema version: 1.0.  Breaking changes require a version bump and a new
dataset version. Additive changes (new optional fields) are backward-compatible.

Producer responsibility:
- Map domain-level operation strings to the normalized Operation literal:
    'complexity_evaluation' -> 'assess'
    'task_decomposition'   -> 'decompose'
    'worker_execution'     -> 'worker_execution' (unchanged)
    'verification'         -> 'verification'     (unchanged)
    'context_condense'     -> 'context_condense' (unchanged)
- Uppercase role names before constructing NormalizedEvent. The domain emits
  AgentRole values as lowercase ('boss', 'manager', 'worker', 'pending'); the
  schema's Role literal is uppercase ('BOSS', 'MANAGER', 'WORKER', 'PENDING').
  Producers MUST apply AgentRole(...).value.upper() (or equivalent) before
  constructing a NormalizedEvent.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field


SCHEMA_VERSION = "1.0"

EventType = Literal[
    "run_started",
    "run_completed",
    "agent_created",
    "prompt_sent",
    "llm_response",
    "tokens_consumed",
    "tool_use",
    "tool_result",
    "worker_cost_recorded",
    "status_changed",
    "subtasks_defined",
    "child_spawned",
    "work_completed",
    "work_failed",
    "retry_scheduled",
    "verification_failed",
    "redecomposition_triggered",
]

Source = Literal["tree", "flat_cli"]

Role = Literal["BOSS", "MANAGER", "WORKER", "PENDING", "JUDGE", "CONDENSER", "FLAT"]

Operation = Literal[
    "assess",
    "decompose",
    "worker_execution",
    "verification",
    "context_condense",
]


class PromptSentPayload(BaseModel):
    model_config = {"frozen": True}
    prompt_text: str
    prompt_type: Operation
    model: str


class TokensConsumedPayload(BaseModel):
    model_config = {"frozen": True}
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    thinking_tokens: int | None = None
    cost_usd: float
    operation: Operation
    model: str | None = None


class ToolUsePayload(BaseModel):
    model_config = {"frozen": True}
    call_id: str | None = None
    tool_name: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None


class ToolResultPayload(BaseModel):
    model_config = {"frozen": True}
    call_id: str | None = None
    result_text: str
    was_truncated: bool = False
    result_bytes: int | None = None
    is_error: bool = False


class AgentCreatedPayload(BaseModel):
    model_config = {"frozen": True}
    role: Role
    task_description: str | None = None
    depth: int = 0


class StatusChangedPayload(BaseModel):
    model_config = {"frozen": True}
    old_status: str
    new_status: str


class SubtasksDefinedPayload(BaseModel):
    model_config = {"frozen": True}
    subtask_count: int


class ChildSpawnedPayload(BaseModel):
    model_config = {"frozen": True}
    child_id: str
    description: str


class RunLifecyclePayload(BaseModel):
    model_config = {"frozen": True}
    root_id: str | None = None
    status: str | None = None
    duration_seconds: float | None = None


class VerificationFailedPayload(BaseModel):
    """Mirrors core.domain.events.events.VerificationFailed."""

    model_config = {"frozen": True}
    failed_stage: str  # "structural", "deterministic", "execution", "judge"
    feedback: str
    stages_passed: list[str] = Field(default_factory=list)


class RetryScheduledPayload(BaseModel):
    """Mirrors core.domain.events.events.RetryScheduled."""

    model_config = {"frozen": True}
    attempt: int  # 1-indexed retry attempt number
    reason: str
    escalated_model: str | None = None
    is_verification_retry: bool = False


class WorkerCostRecordedPayload(BaseModel):
    """Mirrors core.domain.events.events.WorkerCostRecorded.

    Token fields are optional because some worker tools (e.g., PTY-based
    execution) do not expose token counts.
    """

    model_config = {"frozen": True}
    tool_name: str  # "claude_code", "openhands"
    model: str | None = None
    tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


class GenericPayload(BaseModel):
    """Fallback for event types whose payload doesn't have a dedicated model yet."""

    model_config = {"frozen": True}
    data: dict[str, Any] = Field(default_factory=dict)


# Payload union dispatch: Pydantic v2 "smart union" matches the FIRST model
# whose required fields validate. Because RunLifecyclePayload (and several
# other members) have only optional fields, an empty {} payload will
# deserialize as the first all-optional member (NOT GenericPayload).
# DOWNSTREAM CONSUMERS MUST DISPATCH BY event_type, NOT by
# isinstance(payload, ...). The Union ordering here is informational only;
# do not rely on it for semantic dispatch.
Payload = (
    PromptSentPayload
    | TokensConsumedPayload
    | ToolUsePayload
    | ToolResultPayload
    | AgentCreatedPayload
    | StatusChangedPayload
    | SubtasksDefinedPayload
    | ChildSpawnedPayload
    | RunLifecyclePayload
    | VerificationFailedPayload
    | RetryScheduledPayload
    | WorkerCostRecordedPayload
    | GenericPayload
)


class NormalizedEvent(BaseModel):
    """Single normalized event -- one line of events.jsonl."""

    model_config = {"frozen": True}

    event_id: str
    run_id: str
    occurred_at: AwareDatetime
    event_type: EventType
    source: Source
    agent_id: str | None = None
    parent_agent_id: str | None = None
    role: Role
    depth: int = 0
    sequence_number: int = 0
    payload: Payload


class RunMeta(BaseModel):
    """Per-run metadata -- the meta.json file."""

    model_config = {"frozen": True}

    run_id: str
    cve_id: str
    cell: Literal["A1", "A2", "A3", "A4", "B1", "B2"]
    replicate: int = 0
    system: Source
    domain_briefing_enabled: bool
    subagent_enabled: bool | None
    prompt_strategy: Literal["secbench", "null", "cli_default"]
    docker_image: str
    budget_usd_cap: float
    wallclock_sec_cap: int
    models: dict[str, str | None]
    started_at: AwareDatetime
    ended_at: AwareDatetime
    wallclock_seconds: float
    termination_reason: Literal[
        "completed",
        "budget_cap",
        "wallclock_cap",
        "llm_error",
        "container_error",
        "tree_timeout",
    ]
    code_sha: dict[str, str]
    env: dict[str, str]
    dataset_schema_version: str = SCHEMA_VERSION
    notes: str = ""


class IndexEntry(BaseModel):
    """One line of INDEX.jsonl -- run-level summary for fast filtering."""

    model_config = {"frozen": True}

    run_id: str
    cve_id: str
    cell: str
    replicate: int
    path: str
    termination_reason: str
    wallclock_seconds: float
    total_cost_usd: float
    event_count: int
    artifacts_produced: list[str] = Field(default_factory=list)
    mechanical_pass: dict[str, bool] = Field(default_factory=dict)
    audit_violations: int = 0
