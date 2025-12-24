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

    model_config = {"from_attributes": True}


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


class SubtaskJustificationSchema(BaseModel):
    """Schema for supervisor's justification of a subtask."""

    parent_task: str = Field("", description="The supervisor's received task being decomposed")
    split_reason: str = Field("", description="Why this subtask was split from the parent")
    objective: str = Field("", description="What this subtask aims to achieve")
    plan: str = Field("", description="How this subtask will be executed")
    why_it_may_work: str = Field("", description="Reasoning for why this approach should succeed")
    expected_results: str = Field("", description="What outputs/outcomes are expected")

    # Budget allocation reasoning
    budget_allocation: str = Field("", description="Percentage of total project budget and computation")
    complexity_assessment: str = Field("", description="Simple/moderate/complex with concrete justification")
    significance_weight: str = Field("", description="How significant relative to siblings (critical path, importance)")
    resource_justification: str = Field("", description="Why this budget percentage is appropriate for the task scope")


class WorkerReportSchema(BaseModel):
    """Schema for worker's report upon completing a task."""

    original_task: str = Field("", description="The original task that was assigned")
    approach: str = Field("", description="How the worker approached the task")
    reasoning: str = Field("", description="Why this approach was chosen and why it should work")
    deliverables: str = Field("", description="Summary of what was produced/delivered")
    challenges: str = Field("", description="Any challenges encountered and how they were addressed")


class ChildWorkerReportSchema(BaseModel):
    """Schema for a child worker's report with agent context."""

    agent_id: str = Field(..., description="Worker agent ID (short)")
    task: str = Field("", description="Task description")
    status: str = Field("", description="Agent status")
    report: WorkerReportSchema | None = Field(None, description="Worker's report")


class AggregatedSummarySchema(BaseModel):
    """Aggregated summary of all subordinates' work for supervisor/BOSS nodes."""

    total_workers: int = Field(0, description="Total number of workers in subtree")
    completed_workers: int = Field(0, description="Number of completed workers")
    failed_workers: int = Field(0, description="Number of failed workers")
    combined_deliverables: str = Field("", description="Combined summary of all deliverables")
    combined_approach: str = Field("", description="Combined summary of approaches taken")
    key_challenges: str = Field("", description="Key challenges encountered across all workers")


class SubtaskSummarySchema(BaseModel):
    """Schema for a subtask in the agent summary."""

    description: str = Field(..., description="Subtask description")
    justification: SubtaskJustificationSchema = Field(
        default_factory=SubtaskJustificationSchema,
        description="Supervisor's reasoning for this subtask",
    )
    budget_weight: float = Field(1.0, description="Relative budget weight for this subtask")
    child_id: str | None = Field(None, description="Child agent UUID assigned to this subtask")
    child_status: str | None = Field(None, description="Child agent status")


class TaskQueueItemSchema(BaseModel):
    """Schema for a task in the queue."""

    description: str = Field(..., description="Task description")
    priority: int = Field(0, description="Task priority (0 = normal)")


class BudgetInfoSchema(BaseModel):
    """Schema for budget information."""

    current_budget: float = Field(0.0, description="Current budget balance")
    initial_budget: float = Field(0.0, description="Initial budget allocated")
    spent: float = Field(0.0, description="Budget spent (initial - current)")
    source: str | None = Field(None, description="Budget source (initial/parent)")


# =============================================================================
# Context Dashboard Schemas (Cross-Session Knowledge Sharing)
# =============================================================================


class ContextEntrySchema(BaseModel):
    """Schema for a context entry from the context dashboard."""

    entry_id: str = Field(..., description="Context entry UUID")
    work_title: str = Field(..., description="Concise key describing the completed work")
    objective: str = Field(..., description="What the task aimed to achieve")
    justification: str = Field(..., description="Why this task was assigned")
    work_analysis: str = Field(..., description="Comprehensive analysis of how work was done")
    approach: str | None = Field(None, description="How the worker approached the task")
    challenges: str | None = Field(None, description="Challenges encountered")
    created_at: datetime = Field(..., description="When this entry was created")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")


class PublishedContextSchema(BaseModel):
    """Schema for context published by a supervisor - full key-value submission."""

    entry_id: str = Field(..., description="Context entry UUID")
    work_title: str = Field(..., description="Concise key describing the completed work")
    worker_id: str = Field(..., description="Worker that completed the task")
    objective: str = Field(..., description="What the task aimed to achieve")
    justification: str = Field(..., description="Why this task was assigned")
    work_analysis: str = Field(..., description="Comprehensive analysis of how work was done")
    approach: str | None = Field(None, description="Worker's approach from report")
    challenges: str | None = Field(None, description="Challenges encountered")
    tags: list[str] = Field(default_factory=list, description="Tags for categorization")
    published_at: datetime = Field(..., description="When this was published")


class InheritedContextSchema(BaseModel):
    """Schema for context inherited by a worker from previous sessions."""

    entries: list[ContextEntrySchema] = Field(
        default_factory=list,
        description="Context entries that were relevant to this worker's task",
    )
    total_available: int = Field(
        0, description="Total entries available in dashboard when queried"
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

    # Worker report (for WORKER agents)
    worker_report: WorkerReportSchema | None = Field(
        None, description="Worker's report justifying their work"
    )

    # Child worker reports (for MANAGER/BOSS agents - accumulated from subtree)
    child_worker_reports: list[ChildWorkerReportSchema] = Field(
        default_factory=list,
        description="Accumulated worker reports from all workers in subtree",
    )

    # Aggregated summary (for MANAGER/BOSS agents - synthesized from all subordinates)
    aggregated_summary: AggregatedSummarySchema | None = Field(
        None,
        description="Synthesized summary of all subordinates' work",
    )

    # Budget information
    budget: BudgetInfoSchema | None = Field(None, description="Budget allocation and usage")

    # Task queue
    task_queue: list[TaskQueueItemSchema] = Field(
        default_factory=list, description="Pending tasks in queue"
    )
    queue_size: int = Field(0, description="Number of tasks in queue")

    # Context dashboard - published by supervisor (for BOSS/MANAGER)
    published_context: list[PublishedContextSchema] = Field(
        default_factory=list,
        description="Context entries this supervisor published to the dashboard",
    )

    # Context dashboard - inherited by worker (for WORKER)
    inherited_context: InheritedContextSchema | None = Field(
        None,
        description="Context this worker inherited from previous sessions",
    )


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
    retry_delay: float = Field(..., description="Delay between retries (seconds)")
    poll_interval: float = Field(..., description="Agent polling interval (seconds)")
    llm_timeout: float = Field(..., description="LLM query timeout (seconds)")
    worker_timeout: float = Field(..., description="Worker task timeout (seconds)")
    default_task_complexity_threshold: int = Field(
        ..., description="Complexity threshold for task decomposition"
    )


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
# Tree Work Report Schemas
# =============================================================================


class WorkerReportItemSchema(BaseModel):
    """Schema for a single worker's report in the tree summary."""

    agent_id: str = Field(..., description="Short agent ID")
    task: str = Field("", description="Task description (truncated)")
    original_task: str = Field("", description="Original task assigned")
    approach: str = Field("", description="How the worker approached the task")
    reasoning: str = Field("", description="Why this approach was chosen")
    deliverables: str = Field("", description="What was delivered")
    challenges: str = Field("", description="Challenges encountered")
    result: str = Field("", description="Final result (truncated)")
    depth: int = Field(0, description="Depth in hierarchy")


class TreeStatisticsSchema(BaseModel):
    """Schema for tree statistics."""

    total_agents: int = Field(0, description="Total number of agents")
    completed_agents: int = Field(0, description="Number of completed agents")
    failed_agents: int = Field(0, description="Number of failed agents")
    worker_count: int = Field(0, description="Number of workers")
    manager_count: int = Field(0, description="Number of managers")
    max_depth: int = Field(0, description="Maximum depth of hierarchy")


class TreeWorkReportSchema(BaseModel):
    """Comprehensive work report for a BOSS agent's subtree."""

    boss_id: str = Field(..., description="BOSS agent ID")
    task: str = Field(..., description="Original task")
    status: str = Field(..., description="Overall status")
    statistics: TreeStatisticsSchema = Field(
        default_factory=TreeStatisticsSchema, description="Tree statistics"
    )
    worker_reports: list[WorkerReportItemSchema] = Field(
        default_factory=list, description="All worker reports"
    )
    aggregated_deliverables: str = Field("", description="Aggregated summary of deliverables")
