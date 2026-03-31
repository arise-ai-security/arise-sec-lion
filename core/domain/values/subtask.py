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

    # Task classification
    task_type: str = Field(
        default="general",
        description="Category: general, research, implementation, testing, analysis, etc.",
    )

    # Parent's reasoning for assigning this subtask (Design Choice 4).
    # Captured from LLM decomposition output and passed to child via Briefing.
    justification: dict[str, str] = Field(
        default_factory=dict,
        description="Parent's reasoning for this subtask (e.g. objective, plan, domain context)",
    )

    # Structured child scoping — machine-readable context for targeted recon.
    # All optional with empty defaults so old-style prose-only subtasks work.
    target_paths: tuple[str, ...] = Field(
        default=(),
        description="File/directory paths the child should focus on",
    )
    symbols: tuple[str, ...] = Field(
        default=(),
        description="Function/class/method names relevant to this subtask",
    )
    search_hints: tuple[str, ...] = Field(
        default=(),
        description="Keywords or patterns to search for (CVE IDs, error strings, etc.)",
    )
