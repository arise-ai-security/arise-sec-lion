"""LiteLLM adapter: unified interface to multiple LLM providers.

This adapter wraps LiteLLM to provide a unified interface for querying
various LLM providers (OpenAI, Anthropic, Google, etc.) with optional
cost tracking via the CostCalculatorPort.
"""

import logging
from typing import Any

import litellm

# Drop unsupported params for models that don't support them (e.g., o3 series only supports temperature=1)
litellm.drop_params = True

from core.domain.exceptions import LLMError
from core.domain.llm_response import LLMResponse, LLMUsage
from core.ports.cost_calculator_port import CostCalculatorPort
from core.ports.llm_port import LLMPort

logger = logging.getLogger(__name__)

# Fallback model chains: primary -> list of fallbacks to try in order
MODEL_FALLBACK_CHAINS: dict[str, list[str]] = {
    # OpenAI o3 series
    "o3": ["gpt-4o", "gpt-4o-mini"],
    "o3-mini": ["gpt-4o-mini", "gpt-4o"],
    "openai/o3": ["openai/gpt-4o", "openai/gpt-4o-mini"],
    "openai/o3-mini": ["openai/gpt-4o-mini", "openai/gpt-4o"],
    # Anthropic Claude models (need anthropic/ prefix for litellm)
    "claude-sonnet-4-5-20250514": ["anthropic/claude-sonnet-4-5-20250514", "anthropic/claude-3-5-sonnet-20241022", "gpt-4o", "gpt-4o-mini"],
    "anthropic/claude-sonnet-4-5-20250514": ["anthropic/claude-3-5-sonnet-20241022", "gpt-4o", "gpt-4o-mini"],
    "claude-3-5-sonnet-20241022": ["anthropic/claude-3-5-sonnet-20241022", "gpt-4o", "gpt-4o-mini"],
    "anthropic/claude-3-5-sonnet-20241022": ["gpt-4o", "gpt-4o-mini"],
    # Google Gemini models
    "gemini-2.0-flash": ["gemini/gemini-2.0-flash", "gemini/gemini-1.5-flash", "gpt-4o-mini"],
    "gemini/gemini-2.0-flash": ["gemini/gemini-1.5-flash", "gpt-4o-mini"],
    "gemini-1.5-pro": ["gemini/gemini-1.5-pro", "gpt-4o", "gpt-4o-mini"],
    "gemini/gemini-1.5-pro": ["gpt-4o", "gpt-4o-mini"],
}


def _get_models_to_try(model: str) -> list[str]:
    """Get list of models to try, starting with the requested model."""
    models = [model]
    if model in MODEL_FALLBACK_CHAINS:
        models.extend(MODEL_FALLBACK_CHAINS[model])
    return models


def _is_truncated_json(content: str) -> bool:
    """Check if response looks like truncated JSON."""
    content = content.strip()
    if not content:
        return True
    # Check for common JSON truncation patterns
    if content.startswith("{") and not content.endswith("}"):
        return True
    if content.startswith("[") and not content.endswith("]"):
        return True
    # Check for truncated string (ends with unclosed quote)
    if content.endswith('"') and content.count('"') % 2 != 0:
        return True
    return False


class LiteLLMAdapter(LLMPort):
    """Query LLMs via LiteLLM (OpenAI, Anthropic, etc.) with cost tracking."""

    def __init__(
        self,
        default_config: dict[str, Any] | None = None,
        cost_calculator: CostCalculatorPort | None = None,
    ) -> None:
        """Initialize LiteLLM adapter.

        Args:
            default_config: Default LLM configuration (model, temperature, etc.).
            cost_calculator: Optional cost calculator for pricing. If not provided,
                query_with_usage will use LiteLLM's built-in cost calculation.
        """
        self.default_config = default_config or {}
        self._cost_calculator = cost_calculator

    async def _call_llm(
        self,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        top_p: float | None,
    ) -> Any:
        """Make the actual LLM call. Returns the raw response."""
        return await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
        )

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "o3")
        temperature = merged_config.get("temperature", 0.7)
        max_tokens = merged_config.get("max_tokens", 4000)
        top_p = merged_config.get("top_p")

        # Try primary model first, then fallbacks in order
        models_to_try = _get_models_to_try(model)

        last_error = None
        for current_model in models_to_try:
            try:
                response = await self._call_llm(
                    current_model, prompt, temperature, max_tokens, top_p
                )

                content = response.choices[0].message.content
                if content is None or content.strip() == "":
                    raise LLMError(f"LLM returned empty response for model '{current_model}'")

                # Check for truncated JSON responses (common with some models)
                if _is_truncated_json(content):
                    raise LLMError(f"LLM returned truncated response for model '{current_model}': {content[:100]}...")

                if current_model != model:
                    logger.warning(f"Used fallback model '{current_model}' instead of '{model}'")

                return content

            except litellm.exceptions.AuthenticationError as e:
                last_error = LLMError(
                    f"LLM authentication failed for model '{current_model}': {e}",
                    original_error=e,
                )
                # Don't retry on auth errors - they won't succeed with fallback either
                raise last_error from e

            except (
                litellm.exceptions.RateLimitError,
                litellm.exceptions.APIError,
                litellm.exceptions.Timeout,
                litellm.exceptions.ServiceUnavailableError,
            ) as e:
                last_error = LLMError(
                    f"LLM error for model '{current_model}': {e}",
                    original_error=e,
                )
                if current_model != models_to_try[-1]:
                    logger.warning(f"Model '{current_model}' failed, trying fallback: {e}")
                    continue
                raise last_error from e

            except LLMError:
                # Re-raise our own errors (like empty response)
                if current_model != models_to_try[-1]:
                    logger.warning(f"Model '{current_model}' returned empty, trying fallback")
                    continue
                raise

            except Exception as e:
                last_error = LLMError(
                    f"Unexpected LLM error for model '{current_model}': {e}",
                    original_error=e,
                )
                if current_model != models_to_try[-1]:
                    logger.warning(f"Model '{current_model}' failed unexpectedly, trying fallback: {e}")
                    continue
                raise last_error from e

        # Should not reach here, but just in case
        if last_error:
            raise last_error
        raise LLMError(f"All models failed for query")

    async def query_with_usage(self, prompt: str, config_dict: dict[str, Any]) -> LLMResponse:
        """Query LLM and return response with usage and cost metadata.

        This method extracts token usage from the LLM response and calculates
        cost using either the injected CostCalculatorPort or LiteLLM's built-in
        cost calculation.

        Args:
            prompt: The prompt to send to the LLM.
            config_dict: Configuration including model, temperature, max_tokens.

        Returns:
            LLMResponse with content, usage, model, and cost_usd.

        Raises:
            LLMError: On API failure or timeout.
        """
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "o3")
        temperature = merged_config.get("temperature", 0.7)
        max_tokens = merged_config.get("max_tokens", 4000)
        top_p = merged_config.get("top_p")

        # Try primary model first, then fallbacks in order
        models_to_try = _get_models_to_try(model)

        last_error = None
        for current_model in models_to_try:
            try:
                response = await self._call_llm(
                    current_model, prompt, temperature, max_tokens, top_p
                )

                content = response.choices[0].message.content
                if content is None or content.strip() == "":
                    raise LLMError(f"LLM returned empty response for model '{current_model}'")

                # Check for truncated JSON responses (common with some models)
                if _is_truncated_json(content):
                    raise LLMError(f"LLM returned truncated response for model '{current_model}': {content[:100]}...")

                if current_model != model:
                    logger.warning(f"Used fallback model '{current_model}' instead of '{model}'")

                # Extract usage from response
                usage = response.usage
                prompt_tokens = usage.prompt_tokens if usage else 0
                completion_tokens = usage.completion_tokens if usage else 0
                total_tokens = usage.total_tokens if usage else 0

                # Calculate cost
                cost_usd = self._calculate_cost(current_model, prompt_tokens, completion_tokens, response)

                return LLMResponse(
                    content=content,
                    usage=LLMUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    ),
                    model=current_model,  # Report actual model used
                    cost_usd=cost_usd,
                )

            except litellm.exceptions.AuthenticationError as e:
                last_error = LLMError(
                    f"LLM authentication failed for model '{current_model}': {e}",
                    original_error=e,
                )
                # Don't retry on auth errors
                raise last_error from e

            except (
                litellm.exceptions.RateLimitError,
                litellm.exceptions.APIError,
                litellm.exceptions.Timeout,
                litellm.exceptions.ServiceUnavailableError,
            ) as e:
                last_error = LLMError(
                    f"LLM error for model '{current_model}': {e}",
                    original_error=e,
                )
                if current_model != models_to_try[-1]:
                    logger.warning(f"Model '{current_model}' failed, trying fallback: {e}")
                    continue
                raise last_error from e

            except LLMError:
                if current_model != models_to_try[-1]:
                    logger.warning(f"Model '{current_model}' returned empty, trying fallback")
                    continue
                raise

            except Exception as e:
                last_error = LLMError(
                    f"Unexpected LLM error for model '{current_model}': {e}",
                    original_error=e,
                )
                if current_model != models_to_try[-1]:
                    logger.warning(f"Model '{current_model}' failed unexpectedly, trying fallback: {e}")
                    continue
                raise last_error from e

        if last_error:
            raise last_error
        raise LLMError(f"All models failed for query")

    def _calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        response: Any,
    ) -> float:
        """Calculate cost using injected calculator or LiteLLM fallback.

        Args:
            model: Model name.
            prompt_tokens: Number of input tokens.
            completion_tokens: Number of output tokens.
            response: Raw LiteLLM response (for built-in cost calculation).

        Returns:
            Cost in USD.
        """
        # Use injected calculator if available
        if self._cost_calculator is not None:
            return self._cost_calculator.calculate_llm_cost(model, prompt_tokens, completion_tokens)

        # Fall back to LiteLLM's built-in cost calculation
        try:
            cost = litellm.completion_cost(completion_response=response)
            return round(cost, 6)
        except Exception:
            # If LiteLLM cost calculation fails, return 0
            return 0.0
