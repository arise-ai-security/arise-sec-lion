"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """Immutable subtask with description and child agent config.

    budget_weight: Relative weight for proportional budget allocation (Design Choice 3).
                   Used to calculate child budget as: parent_budget * (weight / total_weights)
    """

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    config: dict[str, Any]
    budget_weight: float = Field(default=1.0, ge=0.0)  # Default equal weight
