"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class SubtaskJustification(BaseModel):
    """Supervisor's justification for creating a subtask."""

    model_config = {"frozen": True}

    # Defaults for backward compatibility with old events that lack justification
    parent_task: str = Field(default="(legacy event)", description="The supervisor's received task being decomposed")
    split_reason: str = Field(default="(legacy event)", description="Why this subtask was split from the parent task")
    objective: str = Field(default="(legacy event)", description="What this subtask aims to achieve")
    plan: str = Field(default="(legacy event)", description="How this subtask will be executed")
    why_it_may_work: str = Field(default="(legacy event)", description="Reasoning for why this approach should succeed")
    expected_results: str = Field(default="(legacy event)", description="What outputs/outcomes are expected")


class Subtask(BaseModel):
    """Immutable subtask with description, justification, and child agent config.

    The budget_weight field represents the relative resource allocation
    for this subtask based on complexity, importance, and estimated time.
    Higher weights get proportionally more budget.
    """

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    # Default factory for backward compatibility with old events
    justification: SubtaskJustification = Field(
        default_factory=SubtaskJustification,
        description="Supervisor's reasoning for this subtask",
    )
    config: dict[str, Any]
    budget_weight: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Relative budget weight (1.0 = normal, higher = more resources)",
    )
