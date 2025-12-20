"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """Immutable subtask with description and child agent config.

    The budget_weight field represents the relative resource allocation
    for this subtask based on complexity, importance, and estimated time.
    Higher weights get proportionally more budget.
    """

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    config: dict[str, Any]
    budget_weight: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Relative budget weight (1.0 = normal, higher = more resources)",
    )
