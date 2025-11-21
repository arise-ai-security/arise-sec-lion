"""Subtask value object for task decomposition.

This module defines the Subtask value object representing an atomic unit
of work created during task decomposition by MANAGER agents.

Parent-Controlled Child Configs:
    Each subtask includes the configuration for the child agent that will execute it.
    This allows parents to specify:
    - Which LLM model to use for each operation (complexity eval, task decomposition)
    - Hyperparameters (temperature, max_tokens) per operation
    - Which tool to use (claude_code or openhands) for workers

    The config is stored as a dict (serialized AgentConfig) to maintain
    compatibility with event sourcing (JSON serialization).
"""

from typing import Any

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """Value object representing a subtask in task decomposition.

    MANAGER agents decompose tasks into subtasks. Each subtask represents
    an atomic unit of work that will be assigned to a child agent, along
    with the configuration specifying how the child should operate.

    This is a value object (immutable, defined by attributes):
        - Immutable (frozen=True)
        - Compared by value, not identity
        - Hashable (can be used in sets/dicts)
        - Self-validating via Pydantic

    Attributes:
        description: Human-readable description of the subtask (non-empty).
        config: Serialized AgentConfig dict specifying child agent configuration.
                Must include "strategy" field and all required config parameters.
                This allows the parent to control which models/hyperparameters
                the child uses for different operations.

    Example:
        subtask = Subtask(
            description="Research BeautifulSoup documentation",
            config={
                "strategy": "heuristic",
                "base": {
                    "model": "gpt-4o-mini",
                    "temperature": 0.5,
                    "max_tokens": 500
                },
                "tool": "claude_code"
            }
        )
    """

    model_config = {"frozen": True}  # Immutable value object

    description: str = Field(..., min_length=1, description="Subtask description")
    config: dict[str, Any] = Field(
        ...,
        description="Serialized AgentConfig dict for child agent (must include 'strategy' field)",
    )
