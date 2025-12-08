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
