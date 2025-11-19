"""Subtask value object for task decomposition.

This module defines the Subtask value object representing an atomic unit
of work created during task decomposition by MANAGER agents.
"""

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """Value object representing a subtask in task decomposition.

    MANAGER agents decompose tasks into subtasks. Each subtask represents
    an atomic unit of work that will be assigned to a WORKER agent.

    This is a value object (immutable, defined by attributes):
        - Immutable (frozen=True)
        - Compared by value, not identity
        - Hashable (can be used in sets/dicts)
        - Self-validating via Pydantic

    Attributes:
        description: Human-readable description of the subtask (non-empty).
    """

    model_config = {"frozen": True}  # Immutable value object

    description: str = Field(..., min_length=1, description="Subtask description")
