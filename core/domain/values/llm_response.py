"""Value objects for LLM responses with usage tracking.

These immutable value objects capture both content and cost metadata
from LLM calls, enabling cost tracking through event sourcing.
"""

from pydantic import BaseModel, Field


class LLMUsage(BaseModel):
    """Token usage from an LLM response."""

    model_config = {"frozen": True}

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class LLMResponse(BaseModel):
    """LLM response with content and cost metadata.

    Captures response content and usage metrics for cost tracking.
    """

    model_config = {"frozen": True}

    content: str
    usage: LLMUsage
    model: str
    cost_usd: float = Field(ge=0.0)
