"""Subtask value object for task decomposition."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """Immutable subtask with description, config, and optional scheduling metadata.

    All new fields have defaults so existing subtasks (from historical events
    and LLM responses without these fields) continue to work unchanged.
    """

    model_config = {"frozen": True}

    # Required (existing)
    description: str = Field(..., min_length=1)
    config: dict[str, Any]

    # DAG scheduling (Phase 2 will use these)
    depends_on: list[int] = Field(
        default_factory=list,
        description="Indices of sibling subtasks this depends on (0-indexed)",
    )
    dependency_type: Literal["finish_to_start", "data", "none"] = Field(
        default="finish_to_start",
        description="Type of dependency relationship",
    )

    # Complexity & verification hints
    estimated_complexity: Literal["simple", "complex", "unknown"] = Field(
        default="unknown",
        description="LLM's estimate of subtask complexity",
    )
    success_criteria: str = Field(
        default="",
        description="What constitutes successful completion",
    )
    failure_indicators: list[str] = Field(
        default_factory=list,
        description="Signs that the subtask has failed",
    )

    # Context needs for sibling coordination
    context_needs: list[str] = Field(
        default_factory=list,
        description="What context this subtask needs from completed siblings",
    )

    # Task classification
    task_type: str = Field(
        default="general",
        description="Category: general, research, implementation, testing, analysis, etc.",
    )
