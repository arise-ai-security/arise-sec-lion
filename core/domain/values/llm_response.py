"""Value objects for LLM responses with usage tracking.

These immutable value objects capture both content and cost metadata
from LLM calls, enabling cost tracking through event sourcing.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMUsage:
    """Token usage from an LLM response.

    Attributes:
        prompt_tokens: Number of tokens in the input prompt.
        completion_tokens: Number of tokens in the generated response.
        total_tokens: Sum of prompt and completion tokens.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        """Validate token counts are non-negative."""
        if self.prompt_tokens < 0:
            raise ValueError(f"prompt_tokens must be >= 0, got {self.prompt_tokens}")
        if self.completion_tokens < 0:
            raise ValueError(f"completion_tokens must be >= 0, got {self.completion_tokens}")
        if self.total_tokens < 0:
            raise ValueError(f"total_tokens must be >= 0, got {self.total_tokens}")


@dataclass(frozen=True)
class LLMResponse:
    """LLM response with content and cost metadata.

    This value object captures both the response content and the
    associated usage metrics for cost tracking.

    Attributes:
        content: The generated text response.
        usage: Token usage breakdown.
        model: Name of the model that generated the response.
        cost_usd: Calculated cost in USD for this call.
    """

    content: str
    usage: LLMUsage
    model: str
    cost_usd: float

    def __post_init__(self) -> None:
        """Validate cost is non-negative."""
        if self.cost_usd < 0:
            raise ValueError(f"cost_usd must be >= 0, got {self.cost_usd}")
