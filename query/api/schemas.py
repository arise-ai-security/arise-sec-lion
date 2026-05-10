"""Pydantic schemas for API request/response models.

These schemas define the JSON structure for API endpoints.
They are separate from domain DTOs to allow API evolution.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class AgentNodeSchema(BaseModel):
    """Schema for a single agent node in the hierarchy tree."""

    id: str = Field(..., description="Agent UUID")
    role: str = Field(..., description="Agent role (BOSS, MANAGER, WORKER, PENDING)")
    status: str = Field(..., description="Current status")
    task_description: str = Field(..., description="Task assigned to this agent")
    parent_id: str | None = Field(None, description="Parent agent UUID")
    restart_count: int = Field(0, description="How many retries/restarts were scheduled")
    was_restarted: bool = Field(False, description="Whether this agent has been restarted at least once")
    hang_restart_count: int = Field(
        0,
        description="How many retries were triggered by hang/no-progress watchdog recovery",
    )
    was_hang_restarted: bool = Field(
        False,
        description="Whether this agent had at least one hang/no-progress recovery restart",
    )
    last_event_at: datetime | None = Field(
        None,
        description="Timestamp of the latest event for this agent",
    )
    idle_seconds: int | None = Field(
        None,
        description="Seconds since latest event for this agent",
    )
    is_stale: bool = Field(
        False,
        description="Whether the agent appears stale (non-terminal and idle beyond threshold)",
    )
    watchdog_phase: str | None = Field(
        None,
        description="Current watchdog phase driving timeout/recovery logic for this agent",
    )
    watchdog_timeout_seconds: int | None = Field(
        None,
        description="Timeout budget for the active watchdog phase",
    )
    watchdog_elapsed_seconds: int | None = Field(
        None,
        description="Elapsed seconds for the active watchdog phase",
    )
    watchdog_overdue: bool = Field(
        False,
        description="Whether elapsed time exceeded the active watchdog timeout",
    )
    watchdog_next_action: str | None = Field(
        None,
        description="Expected next recovery action when watchdog expires",
    )
    children: list["AgentNodeSchema"] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class AgentHierarchySchema(BaseModel):
    """Schema for the complete agent hierarchy tree."""

    root: AgentNodeSchema = Field(..., description="Root agent (BOSS)")
    total_agents: int = Field(..., description="Total number of agents")
    depth: int = Field(..., description="Maximum depth of hierarchy")


class EventSchema(BaseModel):
    """Schema for a single domain event."""

    event_type: str = Field(..., description="Event class name")
    aggregate_id: str = Field(..., description="Agent UUID this event belongs to")
    sequence_number: int = Field(..., description="Event sequence within aggregate")
    occurred_at: datetime = Field(..., description="When the event occurred")
    data: dict = Field(..., description="Event-specific payload")

    model_config = {"from_attributes": True}


class CategorizedEventsSchema(BaseModel):
    """Schema for events categorized by type."""

    received: list[EventSchema] = Field(
        default_factory=list, description="Events received by agent"
    )
    produced: list[EventSchema] = Field(
        default_factory=list, description="Events produced by agent"
    )
    passed: list[EventSchema] = Field(
        default_factory=list, description="Events passed to/from children"
    )
    thinking: list[EventSchema] = Field(default_factory=list, description="Thought capture events")


class AgentListItemSchema(BaseModel):
    """Schema for agent list item (summary view)."""

    id: str
    role: str
    status: str
    task_description: str
    created_at: datetime | None = None
    domain_metadata: dict[str, str | int | float | bool | None] | None = None
    restart_count: int = 0
    was_restarted: bool = False
    hang_restart_count: int = 0
    was_hang_restarted: bool = False

    model_config = {"from_attributes": True}


class PaginationMetaSchema(BaseModel):
    """Schema for pagination metadata."""

    limit: int = Field(..., description="Maximum items per page")
    offset: int = Field(..., description="Number of items skipped")
    total: int = Field(..., description="Total number of items available")
    has_more: bool = Field(..., description="Whether more items exist beyond current page")


class PaginatedAgentListSchema(BaseModel):
    """Schema for paginated agent list response."""

    items: list[AgentListItemSchema] = Field(..., description="List of agents")
    pagination: PaginationMetaSchema = Field(..., description="Pagination metadata")


class PaginatedEventsSchema(BaseModel):
    """Schema for paginated events list response."""

    items: list["EventSchema"] = Field(..., description="List of events")
    pagination: PaginationMetaSchema = Field(..., description="Pagination metadata")


class PromptSchema(BaseModel):
    """Schema for a prompt template."""

    category: str = Field(
        ..., description="Prompt category (system, strategies, tasks, output_formats)"
    )
    name: str = Field(..., description="Template name without extension")
    content: str = Field(..., description="Jinja2 template content")
    updated_at: datetime | None = Field(None, description="Last modification time")


class PromptListSchema(BaseModel):
    """Schema for list of prompts."""

    prompts: list[PromptSchema] = Field(default_factory=list)
    total: int = Field(..., description="Total number of prompts")


class PromptUpdateSchema(BaseModel):
    """Schema for updating a prompt."""

    content: str = Field(..., description="New Jinja2 template content")


class RenderedPromptSchema(BaseModel):
    """Schema for a rendered prompt preview."""

    rendered: str = Field(..., description="Rendered prompt with sample variables")
    variables_used: list[str] = Field(
        default_factory=list, description="Variables found in template"
    )


class PromptVariablesSchema(BaseModel):
    """Schema for available prompt variables."""

    variables: dict[str, str] = Field(..., description="Variable name to description mapping")


# Agent Summary Schemas (CQRS Projection)


class SubtaskSummarySchema(BaseModel):
    """Schema for a subtask in the agent summary."""

    description: str = Field(..., description="Subtask description")
    child_id: str | None = Field(None, description="Child agent UUID assigned to this subtask")
    child_status: str | None = Field(None, description="Child agent status")
    justification: dict[str, str] = Field(
        default_factory=dict,
        description="Parent's reasoning for this subtask (e.g. objective, plan)",
    )


class AncestorSchema(BaseModel):
    """Schema for an ancestor in the agent briefing lineage."""

    role: str = Field(..., description="Ancestor agent role")
    task_summary: str = Field(..., description="Ancestor task summary")


class BriefingSummarySchema(BaseModel):
    """Schema for agent briefing context from parent."""

    parent_task: str = Field(..., description="Parent agent's task description")
    parent_role: str = Field(..., description="Parent agent's role")
    subtask_justification: dict[str, str] = Field(
        default_factory=dict,
        description="Why this task was assigned (e.g. objective, plan, domain context)",
    )
    ancestry: list[AncestorSchema] = Field(
        default_factory=list, description="Lineage chain from root to parent"
    )
    decisions: list[str] = Field(
        default_factory=list, description="Inherited decisions from ancestors"
    )


class AgentSummarySchema(BaseModel):
    """CQRS projection schema for agent node summary.

    Aggregates data from multiple events to provide a comprehensive view
    of an agent's state, configuration, and reasoning.
    """

    id: str = Field(..., description="Agent UUID")
    role: str = Field(..., description="Agent role (BOSS, MANAGER, WORKER, PENDING)")
    status: str = Field(..., description="Current status")
    task_description: str = Field(..., description="Task assigned to this agent")

    # Complexity evaluation (from ComplexityEvaluated event)
    complexity: str | None = Field(None, description="Evaluated complexity (simple/complex)")
    complexity_reasoning: str | None = Field(None, description="LLM reasoning for complexity")

    # For WORKER agents (from CodeGenerationStarted event)
    worker_tool: str | None = Field(None, description="Worker tool used (claude_code/openhands)")

    # For MANAGER agents (from SubtasksDefined event)
    subtasks: list[SubtaskSummarySchema] = Field(
        default_factory=list, description="Subtasks defined by this manager"
    )

    # Briefing context from parent (from AgentCreated event)
    briefing: BriefingSummarySchema | None = Field(
        None, description="Context passed from parent agent (null for BOSS)"
    )

    # Configuration (from AgentConfig)
    config_strategy: str | None = Field(
        None, description="Config strategy (per_operation/heuristic/hybrid)"
    )
    config_details: dict = Field(
        default_factory=dict, description="Configuration details (models, hyperparameters)"
    )

    # Result/Error
    result: str | None = Field(None, description="Final result (if completed)")
    error_message: str | None = Field(None, description="Error message (if failed)")


# System Configuration Schema


class LLMConfigSchema(BaseModel):
    """Schema for LLM configuration."""

    model: str
    temperature: float
    max_tokens: int
    top_p: float | None = None


class InfrastructureConfigSchema(BaseModel):
    """Schema for infrastructure configuration (safe subset)."""

    llm_model_boss: str = Field(..., description="LLM model for BOSS agent")
    worker_tool_type: str = Field(..., description="Default worker tool type")
    worker_tool_model: str = Field(..., description="Model for OpenHands worker")
    worker_tool_timeout: int = Field(..., description="Worker tool timeout (seconds)")


class ApplicationConfigSchema(BaseModel):
    """Schema for application configuration."""

    max_retries: int = Field(..., description="Maximum OCC retry attempts")
    poll_interval: float = Field(..., description="Agent polling interval (seconds)")


class SystemConfigSchema(BaseModel):
    """Schema for system configuration (read-only view)."""

    infrastructure: InfrastructureConfigSchema
    application: ApplicationConfigSchema


# =============================================================================
# Execution Summary Schemas (Cost, Timing, Node Counts)
# =============================================================================


class RoleCostBreakdownSchema(BaseModel):
    """Schema for cost breakdown by agent role."""

    BOSS: float = Field(0.0, description="Total cost from BOSS agents")
    MANAGER: float = Field(0.0, description="Total cost from MANAGER agents")
    WORKER: float = Field(0.0, description="Total cost from WORKER agents")
    PENDING: float = Field(0.0, description="Total cost from PENDING agents")
    UNKNOWN: float = Field(0.0, description="Cost from agents with unknown role")


class RoleCountSchema(BaseModel):
    """Schema for agent counts by role."""

    BOSS: int = Field(0, description="Number of BOSS agents")
    MANAGER: int = Field(0, description="Number of MANAGER agents")
    WORKER: int = Field(0, description="Number of WORKER agents")
    PENDING: int = Field(0, description="Number of PENDING agents")
    total: int = Field(0, description="Total number of agents")


class RoleTokensSchema(BaseModel):
    """Schema for token counts by role."""

    BOSS: int = Field(0, description="Tokens used by BOSS agents")
    MANAGER: int = Field(0, description="Tokens used by MANAGER agents")
    WORKER: int = Field(0, description="Tokens used by WORKER agents")
    PENDING: int = Field(0, description="Tokens used by PENDING agents")


class ExecutionTimingSchema(BaseModel):
    """Schema for execution timing breakdown."""

    total_seconds: float = Field(..., description="Total execution time in seconds")
    by_role: dict[str, float] = Field(
        default_factory=dict, description="Total execution time by role"
    )
    by_phase: dict[str, float] = Field(
        default_factory=dict, description="Time spent in each phase (analyzing, in_progress, etc.)"
    )
    by_agent: dict[str, float] = Field(
        default_factory=dict, description="Execution time per agent (agent_id -> seconds)"
    )


class CostBreakdownSchema(BaseModel):
    """Schema for comprehensive cost breakdown."""

    total_cost_usd: float = Field(..., description="Total cost in USD")
    llm_cost_usd: float = Field(..., description="Cost from LLM calls")
    worker_cost_usd: float = Field(..., description="Cost from worker tool execution")

    # Token usage
    total_tokens: int = Field(..., description="Total tokens consumed")
    prompt_tokens: int = Field(..., description="Input tokens consumed")
    completion_tokens: int = Field(..., description="Output tokens consumed")
    cache_read_tokens: int = Field(0, description="Cache read tokens consumed")
    cache_write_tokens: int = Field(0, description="Cache write tokens consumed")
    reasoning_tokens: int = Field(0, description="Reasoning tokens consumed")

    # Breakdowns
    cost_by_role: RoleCostBreakdownSchema = Field(
        default_factory=RoleCostBreakdownSchema, description="Cost breakdown by agent role"
    )
    cost_by_model: dict[str, float] = Field(
        default_factory=dict, description="Cost breakdown by LLM model"
    )
    cost_by_operation: dict[str, float] = Field(
        default_factory=dict, description="Cost breakdown by operation type"
    )
    cost_by_agent: dict[str, float] = Field(
        default_factory=dict, description="Cost breakdown by agent ID"
    )
    tokens_by_role: RoleTokensSchema = Field(
        default_factory=RoleTokensSchema, description="Token usage by role"
    )

    # Budget tracking
    budget_limit_usd: float | None = Field(None, description="Budget limit if configured")
    budget_remaining_usd: float | None = Field(None, description="Remaining budget")
    budget_exceeded: bool = Field(False, description="Whether budget was exceeded")


class ExecutionSummarySchema(BaseModel):
    """Comprehensive execution summary for an agent hierarchy.

    Combines event statistics, cost breakdown, timing, and node counts.
    This is the main schema for the cost/execution viewer feature.
    """

    # Event statistics
    total_events: int = Field(..., description="Total number of domain events")
    events_by_type: dict[str, int] = Field(
        default_factory=dict, description="Event counts by type name"
    )
    first_event: datetime | None = Field(None, description="Timestamp of first event")
    last_event: datetime | None = Field(None, description="Timestamp of last event")
    error_count: int = Field(0, description="Number of WorkFailed events")

    # Node counts
    node_counts: RoleCountSchema = Field(
        default_factory=RoleCountSchema, description="Agent counts by role"
    )

    # Cost breakdown
    cost: CostBreakdownSchema = Field(..., description="Cost breakdown")

    # Execution timing
    timing: ExecutionTimingSchema = Field(..., description="Execution timing details")

    # Derived fields
    is_complete: bool = Field(False, description="Whether all agents have completed")


# =============================================================================
# Prompt Schemas (for observability)
# =============================================================================


class AgentPromptSchema(BaseModel):
    """Schema for a prompt sent by an agent."""

    prompt: str = Field(..., description="Full prompt text")
    prompt_type: str = Field(
        ...,
        description=(
            "Type of prompt "
            "(complexity_evaluation, task_decomposition, worker_execution)"
        ),
    )
    target: str = Field(
        ...,
        description="Where prompt was sent (llm, claude_code, openhands, etc.)",
    )
    occurred_at: datetime = Field(..., description="When the prompt was sent")


class AgentPromptsSchema(BaseModel):
    """Schema for list of prompts for an agent."""

    agent_id: str = Field(..., description="Agent UUID")
    prompts: list[AgentPromptSchema] = Field(default_factory=list)
    total: int = Field(..., description="Total number of prompts")


# =============================================================================
# Prompt Trace Schemas (for hierarchy visualization)
# =============================================================================


class PromptSectionSchema(BaseModel):
    """Schema for a single parsed prompt section."""

    tag: str = Field(..., description="XML tag name (e.g., 'ROLE', 'parent-context')")
    content: str = Field(..., description="Section content")
    provenance: str = Field(
        ..., description="Source: template|parent|sibling|children|shared|system"
    )


class ParsedPromptSchema(BaseModel):
    """Schema for a prompt with parsed sections grouped by provenance."""

    raw: str = Field(..., description="Original raw prompt text")
    raw_length: int = Field(..., description="Length of raw prompt in characters")
    occurred_at: datetime = Field(..., description="When prompt was sent")
    prompt_type: str = Field(
        ...,
        description="Type: complexity_evaluation|task_decomposition|worker_execution",
    )
    target: str = Field(
        ..., description="Target: llm|claude_code|openhands|google_adk"
    )
    sections: list[PromptSectionSchema] = Field(
        default_factory=list, description="All parsed sections in order"
    )
    sections_by_provenance: dict[str, list[PromptSectionSchema]] = Field(
        default_factory=dict, description="Sections grouped by provenance type"
    )


class TraceAgentNodeSchema(BaseModel):
    """Schema for an agent node in the trace hierarchy tree."""

    agent_id: str = Field(..., description="Agent UUID")
    role: str = Field(..., description="Agent role (boss|manager|worker|pending)")
    depth: int = Field(..., description="Depth in hierarchy (root=0)")
    task: str = Field(..., description="Task description")
    sibling_index: int = Field(..., description="Position among siblings (0-indexed)")
    prompt_count: int = Field(..., description="Number of prompts sent by this agent")
    prompts: list[ParsedPromptSchema] = Field(
        default_factory=list, description="Parsed prompts with provenance"
    )
    children: list["TraceAgentNodeSchema"] = Field(
        default_factory=list, description="Child agent nodes"
    )


class HierarchyTraceSchema(BaseModel):
    """Schema for complete hierarchy trace response."""

    root: TraceAgentNodeSchema = Field(
        ..., description="Root agent node with full tree"
    )
    total_agents: int = Field(..., description="Total agents in hierarchy")
    max_depth: int = Field(..., description="Maximum depth reached")
