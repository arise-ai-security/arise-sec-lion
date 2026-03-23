"""Value objects for LLM responses with usage tracking.

These immutable value objects capture both content and cost metadata
from LLM calls, enabling cost tracking through event sourcing.
"""

from typing import Any

from pydantic import BaseModel, Field


class LLMUsage(BaseModel):
    """Token usage from an LLM response."""

    model_config = {"frozen": True}

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        """Accumulate usage across multiple LLM turns."""
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class LLMResponse(BaseModel):
    """LLM response with content and cost metadata.

    Captures response content and usage metrics for cost tracking.
    """

    model_config = {"frozen": True}

    content: str
    usage: LLMUsage
    model: str
    cost_usd: float = Field(ge=0.0)


# =========================================================================
# Tool-calling value objects
# =========================================================================


class ToolCall(BaseModel):
    """A single tool call requested by the LLM."""

    model_config = {"frozen": True}

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMToolResponse(BaseModel):
    """LLM response that may contain tool calls instead of (or alongside) text.

    Used by query_with_tools — the LLM either returns text content
    (final answer) or tool_calls (requesting tool execution).
    """

    model_config = {"frozen": True}

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: LLMUsage
    model: str
    cost_usd: float = Field(ge=0.0)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
