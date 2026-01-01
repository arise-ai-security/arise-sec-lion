"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class SubtaskJustification(BaseModel):
    """Justification for a subtask with budget reasoning (Design Choice 4).

    Provides rich context about why a subtask was created and how
    resources should be allocated.
    """

    model_config = {"frozen": True}

    # Core justification fields
    objective: str = ""  # What this subtask achieves
    plan: str = ""  # How it will be executed

    # Budget-related fields (Design Choice 4: Context Passing Tree)
    budget_allocation: str = ""  # e.g., "Allocated 182 units (18% of parent's 1000)"
    complexity_assessment: str = ""  # e.g., "Moderate - requires 3 LLM calls"
    significance_weight: str = ""  # e.g., "Critical path - blocks downstream tasks"
    resource_justification: str = ""  # e.g., "Needs tool execution + validation"


class Subtask(BaseModel):
    """Immutable subtask with description, justification, and child agent config.

    budget_weight: Relative weight for proportional budget allocation (Design Choice 3).
                   Used to calculate child budget as: parent_budget * (weight / total_weights)
    justification: Structured justification with budget reasoning (Design Choice 4).
    """

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    config: dict[str, Any]
    budget_weight: float = Field(default=1.0, ge=0.0)  # Default equal weight
    justification: SubtaskJustification = Field(
        default_factory=SubtaskJustification
    )  # Design Choice 4
