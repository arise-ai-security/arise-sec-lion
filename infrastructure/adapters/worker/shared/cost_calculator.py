"""Cost calculation utilities for worker adapters.

Provides model pricing registry and cost calculation helpers.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Per-million-token pricing for a model."""

    input_per_million: float
    output_per_million: float

    def calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate total cost in USD.

        Args:
            input_tokens: Number of input/prompt tokens.
            output_tokens: Number of output/completion tokens.

        Returns:
            Total cost in USD.
        """
        input_cost = (input_tokens / 1_000_000) * self.input_per_million
        output_cost = (output_tokens / 1_000_000) * self.output_per_million
        return input_cost + output_cost


# Pricing registry - update as pricing changes
# Source: Official pricing pages as of 2025
MODEL_PRICING: dict[str, ModelPricing] = {
    # Gemini models
    "gemini-3-pro": ModelPricing(1.25, 5.00),
    "gemini-2.0-flash": ModelPricing(0.10, 0.40),
    "gemini-1.5-pro": ModelPricing(1.25, 5.00),
    "gemini-1.5-flash": ModelPricing(0.075, 0.30),
    # OpenAI models
    "gpt-4o": ModelPricing(2.50, 10.00),
    "gpt-4o-mini": ModelPricing(0.15, 0.60),
    "gpt-4-turbo": ModelPricing(10.00, 30.00),
    "gpt-3.5-turbo": ModelPricing(0.50, 1.50),
    # Anthropic models
    "claude-sonnet-4-20250514": ModelPricing(3.00, 15.00),
    "claude-opus-4-20250514": ModelPricing(15.00, 75.00),
    "claude-3-5-sonnet-20241022": ModelPricing(3.00, 15.00),
    "claude-3-opus-20240229": ModelPricing(15.00, 75.00),
}


def get_model_pricing(model: str) -> ModelPricing:
    """Get pricing for a model.

    Attempts exact match first, then tries without provider prefix.
    Falls back to conservative default pricing if model not found.

    Args:
        model: Model identifier (e.g., "gpt-4o", "openai/gpt-4o").

    Returns:
        ModelPricing for the model.
    """
    # Try exact match
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    # Try without provider prefix (e.g., "openai/gpt-4o" -> "gpt-4o")
    if "/" in model:
        base_model = model.split("/", 1)[1]
        if base_model in MODEL_PRICING:
            return MODEL_PRICING[base_model]

    # Conservative default (high estimate to avoid undercharging)
    return ModelPricing(5.00, 15.00)
