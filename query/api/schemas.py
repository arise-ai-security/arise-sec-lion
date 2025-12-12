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


class SubtaskSummarySchema(BaseModel):
    """Schema for a subtask in the agent summary."""

    description: str = Field(..., description="Subtask description")
    child_id: str | None = Field(None, description="Child agent UUID assigned to this subtask")
    child_status: str | None = Field(None, description="Child agent status")


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
