"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class SubtaskJustification(BaseModel):
    """Supervisor's justification for creating a subtask."""

    model_config = {"frozen": True}

    parent_task: str = Field(..., description="The supervisor's received task being decomposed")
    split_reason: str = Field(..., description="Why this subtask was split from the parent task")
    objective: str = Field(..., description="What this subtask aims to achieve")
    plan: str = Field(..., description="How this subtask will be executed")
    why_it_may_work: str = Field(..., description="Reasoning for why this approach should succeed")
    expected_results: str = Field(..., description="What outputs/outcomes are expected")


class Subtask(BaseModel):
    """Immutable subtask with description, justification, and child agent config.

    The budget_weight field represents the relative resource allocation
    for this subtask based on complexity, importance, and estimated time.
    Higher weights get proportionally more budget.
    """

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    justification: SubtaskJustification = Field(
        ..., description="Supervisor's reasoning for this subtask"
    )
    config: dict[str, Any]
    budget_weight: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Relative budget weight (1.0 = normal, higher = more resources)",
    )
