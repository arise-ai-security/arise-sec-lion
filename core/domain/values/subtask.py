"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class SubtaskJustification(BaseModel):
    """Justification for a subtask with budget reasoning (Design Choice 4).

    Provides rich context about why a subtask was created and how
    resources should be allocated. This justification is passed to child
    agents as part of <SUPERVISOR_EXPECTATIONS> context.
    """

    model_config = {"frozen": True}

    # Core justification fields
    objective: str = ""  # What this subtask achieves
    plan: str = ""  # How it will be executed
    split_reason: str = ""  # Why this was split off/assigned to a sub-agent
    why_it_works: str = ""  # Why the suggested approach should work
    expected_results: str = ""  # Expected deliverables from this subtask

    # Budget-related fields (Design Choice 4: Context Passing Tree)
    budget_allocation: str = ""  # e.g., "40% of budget (weight 1.6 of 4.0)"
    complexity_assessment: str = ""  # e.g., "MODERATE: Requires multi-file tracing"
    significance_weight: str = ""  # e.g., "CRITICAL PATH: Blocks downstream tasks"
    resource_justification: str = ""  # e.g., "Static analysis demands line-by-line reasoning"


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
