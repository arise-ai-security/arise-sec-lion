"""Subtask value object for task decomposition."""

from typing import Any

from pydantic import BaseModel, Field


class Subtask(BaseModel):
    """Immutable subtask with description and child agent config."""

    model_config = {"frozen": True}

    description: str = Field(..., min_length=1)
    config: dict[str, Any]
