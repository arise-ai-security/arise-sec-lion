"""Cost calculator adapter using LiteLLM's pricing data.

This adapter implements CostCalculatorPort using LiteLLM's built-in
model_cost dictionary, which is automatically updated with provider pricing.
Falls back to a static table only for models not in LiteLLM's database.

LiteLLM pricing source:
- https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from core.ports.runtime_ports import CostCalculatorPort


if TYPE_CHECKING:
    from types import ModuleType

logger = logging.getLogger(__name__)

__all__ = ["DefaultCostCalculator", "ModelPricing", "PriceInfo"]


# =============================================================================
# Constants
# =============================================================================

TOKENS_PER_MILLION: int = 1_000_000
COST_DECIMAL_PLACES: int = 6
SECONDS_PER_MINUTE: float = 60.0


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """Immutable pricing per 1M tokens for a model."""

    input_cost_per_million: float
    output_cost_per_million: float

    def calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate total cost for given token counts."""
        input_cost = (prompt_tokens / TOKENS_PER_MILLION) * self.input_cost_per_million
        output_cost = (completion_tokens / TOKENS_PER_MILLION) * self.output_cost_per_million
        return round(input_cost + output_cost, COST_DECIMAL_PLACES)

    def average_cost_per_million(self) -> float:
        """Average of input and output cost (for when split is unknown)."""
        return (self.input_cost_per_million + self.output_cost_per_million) / 2


@dataclass(frozen=True, slots=True)
class PriceInfo:
    """Detailed pricing information for display/debugging."""

    input_per_million: float
    output_per_million: float
    source: str  # "litellm", "fallback", or "default"

    @property
    def is_default(self) -> bool:
        """True if using conservative default pricing."""
        return self.source == "default"


# =============================================================================
# Default Pricing Tables (Fallback)
# =============================================================================

# Used when LiteLLM's own cost map is missing an entry (it currently lacks
# Anthropic 4.x). Prices in USD per 1M tokens, sourced from
# https://platform.claude.com/docs/en/about-claude/pricing (verified 2026-04).
FALLBACK_PRICING: dict[str, ModelPricing] = {
    "gpt-4o": ModelPricing(2.50, 10.00),
    "gpt-4o-mini": ModelPricing(0.15, 0.60),
    "claude-3-5-sonnet-20241022": ModelPricing(3.00, 15.00),
    "claude-3-opus-20240229": ModelPricing(15.00, 75.00),
    # Anthropic 4.x — used across the tree-vs-flat experiment cells.
    "claude-opus-4-7": ModelPricing(5.00, 25.00),
    "claude-sonnet-4-6": ModelPricing(3.00, 15.00),
    "claude-haiku-4-5": ModelPricing(1.00, 5.00),
}

# Conservative default for completely unknown models; logged as a warning so
# experiment operators spot silent misattribution.
DEFAULT_PRICING = ModelPricing(5.00, 15.00)

# Worker tool pricing: USD per minute of execution
WORKER_TOOL_PRICING: dict[str, float] = {
    "claude_code": 0.05,  # ~$3/hour estimate
    "openhands": 0.03,  # ~$1.80/hour estimate
}
DEFAULT_WORKER_RATE_PER_MINUTE: float = 0.04


# =============================================================================
# LiteLLM Integration
# =============================================================================


def _get_litellm() -> ModuleType | None:
    """Lazy import LiteLLM to avoid import errors if not installed."""
    try:
        import litellm

        return litellm
    except ImportError:
        return None


@lru_cache(maxsize=1)
def _get_cached_model_cost_map() -> dict[str, dict] | None:
    """Get LiteLLM's model cost map with caching.

    The cost map is cached because it's expensive to load and rarely changes.
    Cache is invalidated on process restart or manual clear.
    """
    litellm = _get_litellm()
    if litellm is None:
        return None

    try:
        return litellm.get_model_cost_map(url="")
    except Exception as e:
        logger.warning("Failed to load LiteLLM cost map: %s", e)
        return None


def _normalize_model_name(model: str) -> str:
    """Strip provider prefix from model name (e.g., 'openai/gpt-4' -> 'gpt-4')."""
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def _lookup_litellm_pricing(model: str) -> ModelPricing | None:
    """Look up model pricing from LiteLLM's cost map.

    Tries exact match first, then without provider prefix.
    Returns None if model not found.
    """
    cost_map = _get_cached_model_cost_map()
    if cost_map is None:
        return None

    # Try exact match first
    candidates = [model]
    if "/" in model:
        candidates.append(_normalize_model_name(model))

    for candidate in candidates:
        if candidate in cost_map:
            info = cost_map[candidate]
            input_per_token = info.get("input_cost_per_token", 0)
            output_per_token = info.get("output_cost_per_token", 0)

            if input_per_token == 0 and output_per_token == 0:
                continue  # Skip if pricing is zero (likely missing)

            return ModelPricing(
                input_cost_per_million=input_per_token * TOKENS_PER_MILLION,
                output_cost_per_million=output_per_token * TOKENS_PER_MILLION,
            )

    return None


# =============================================================================
# Cost Calculator Implementation
# =============================================================================


class DefaultCostCalculator(CostCalculatorPort):
    """Calculate costs using LiteLLM's pricing data with fallback.

    Pricing resolution order:
    1. LiteLLM's model_cost dictionary (auto-updated with provider pricing)
    2. Fallback static pricing table
    3. Conservative default pricing

    Example:
        calculator = DefaultCostCalculator()
        cost = calculator.calculate_llm_cost("gpt-4o", prompt_tokens=1000, completion_tokens=500)
    """

    def __init__(
        self,
        fallback_pricing: dict[str, ModelPricing] | None = None,
        worker_pricing: dict[str, float] | None = None,
    ) -> None:
        """Initialize with optional custom pricing tables.

        Args:
            fallback_pricing: Custom fallback model pricing. Defaults to built-in table.
            worker_pricing: Custom worker tool pricing (USD per minute).
        """
        self._fallback_pricing = fallback_pricing or FALLBACK_PRICING
        self._worker_pricing = worker_pricing or WORKER_TOOL_PRICING

    def calculate_llm_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Calculate cost in USD for an LLM call.

        Args:
            model: Model name (with or without provider prefix).
            prompt_tokens: Number of input tokens.
            completion_tokens: Number of output tokens.

        Returns:
            Cost in USD, rounded to 6 decimal places.
        """
        pricing = self._resolve_pricing(model)
        return pricing.calculate_cost(prompt_tokens, completion_tokens)

    def calculate_worker_cost(
        self,
        tool_name: str,
        model: str | None,
        tokens: int | None,
        duration_seconds: float,
    ) -> float:
        """Calculate estimated cost for worker tool execution.

        Uses token-based pricing if available, otherwise time-based.

        Args:
            tool_name: Worker tool identifier ("claude_code", "openhands").
            model: Underlying model if known (for token-based pricing).
            tokens: Total tokens if available.
            duration_seconds: Execution duration.

        Returns:
            Estimated cost in USD, rounded to 6 decimal places.
        """
        # Token-based pricing (more accurate when available)
        if tokens is not None and model is not None:
            pricing = self._resolve_pricing(model)
            # Estimate 50/50 split when only total tokens known
            half_tokens = tokens // 2
            return pricing.calculate_cost(half_tokens, half_tokens)

        # Time-based pricing (fallback)
        rate = self._worker_pricing.get(tool_name, DEFAULT_WORKER_RATE_PER_MINUTE)
        minutes = duration_seconds / SECONDS_PER_MINUTE
        return round(rate * minutes, COST_DECIMAL_PLACES)

    def get_price_info(self, model: str) -> PriceInfo:
        """Get detailed pricing information for a model.

        Useful for debugging and displaying cost breakdowns.

        Args:
            model: Model name to look up.

        Returns:
            PriceInfo with pricing details and source.
        """
        # Try LiteLLM first
        litellm_pricing = _lookup_litellm_pricing(model)
        if litellm_pricing is not None:
            return PriceInfo(
                input_per_million=litellm_pricing.input_cost_per_million,
                output_per_million=litellm_pricing.output_cost_per_million,
                source="litellm",
            )

        # Try fallback
        fallback_pricing = self._lookup_fallback_pricing(model)
        if fallback_pricing is not DEFAULT_PRICING:
            return PriceInfo(
                input_per_million=fallback_pricing.input_cost_per_million,
                output_per_million=fallback_pricing.output_cost_per_million,
                source="fallback",
            )

        # Default
        return PriceInfo(
            input_per_million=DEFAULT_PRICING.input_cost_per_million,
            output_per_million=DEFAULT_PRICING.output_cost_per_million,
            source="default",
        )

    def _resolve_pricing(self, model: str) -> ModelPricing:
        """Resolve pricing for a model using all available sources."""
        # Try LiteLLM first (most accurate when up to date).
        litellm_pricing = _lookup_litellm_pricing(model)
        if litellm_pricing is not None:
            return litellm_pricing

        # Fall back to our own table; warn loudly on default so experiment
        # operators catch silent misattribution when the map ages.
        pricing = self._lookup_fallback_pricing(model)
        if pricing is DEFAULT_PRICING:
            logger.warning(
                "No pricing entry for model %r — using DEFAULT_PRICING "
                "(input=%s/MTok, output=%s/MTok). Cost accounting for this "
                "model will be inaccurate; add it to FALLBACK_PRICING.",
                model,
                DEFAULT_PRICING.input_cost_per_million,
                DEFAULT_PRICING.output_cost_per_million,
            )
        return pricing

    def _lookup_fallback_pricing(self, model: str) -> ModelPricing:
        """Look up pricing from fallback table with fuzzy matching."""
        # Exact match
        if model in self._fallback_pricing:
            return self._fallback_pricing[model]

        # Without provider prefix
        normalized = _normalize_model_name(model)
        if normalized in self._fallback_pricing:
            return self._fallback_pricing[normalized]

        # Partial match (for versioned models like "claude-3-5-sonnet-20241022")
        for known_model, pricing in self._fallback_pricing.items():
            if model.startswith(known_model) or known_model.startswith(model):
                return pricing

        return DEFAULT_PRICING

    @staticmethod
    def clear_cache() -> None:
        """Clear the LiteLLM cost map cache.

        Call this if you need to refresh pricing data without restarting.
        """
        _get_cached_model_cost_map.cache_clear()
