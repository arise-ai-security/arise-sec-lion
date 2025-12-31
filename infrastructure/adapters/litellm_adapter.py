"""LiteLLM adapter: unified interface to multiple LLM providers.

This adapter wraps LiteLLM to provide a unified interface for querying
various LLM providers (OpenAI, Anthropic, Google, etc.) with optional
cost tracking via the CostCalculatorPort.
"""

from typing import Any

import litellm

from core.domain.exceptions import LLMError
from core.domain.values.llm_response import LLMResponse, LLMUsage
from core.ports.cost_calculator_port import CostCalculatorPort
from core.ports.llm_port import LLMPort


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

    async def _execute_completion(
        self,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int,
        top_p: float | None,
    ) -> Any:
        """Execute LiteLLM completion with unified exception handling.

        Args:
            model: Model identifier (e.g., 'gpt-4', 'claude-3-opus').
            prompt: The prompt to send to the LLM.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.
            top_p: Optional nucleus sampling parameter.

        Returns:
            Raw LiteLLM response object.

        Raises:
            LLMError: On any API failure, with specific error messages.
        """
        try:
            return await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
            )

        except litellm.exceptions.AuthenticationError as e:
            raise LLMError(
                f"LLM authentication failed for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.RateLimitError as e:
            raise LLMError(
                f"LLM rate limit exceeded for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.APIError as e:
            raise LLMError(
                f"LLM API error for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.Timeout as e:
            raise LLMError(
                f"LLM request timed out for model '{model}': {e}",
                original_error=e,
            ) from e

        except litellm.exceptions.ServiceUnavailableError as e:
            raise LLMError(
                f"LLM service unavailable for model '{model}': {e}",
                original_error=e,
            ) from e

        except Exception as e:
            raise LLMError(
                f"Unexpected LLM error for model '{model}': {e}",
                original_error=e,
            ) from e

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Query LLM and return response content.

        Args:
            prompt: The prompt to send to the LLM.
            config_dict: Configuration including model, temperature, max_tokens.

        Returns:
            Response content as string.

        Raises:
            LLMError: On API failure or empty response.
        """
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")

        response = await self._execute_completion(
            model=model,
            prompt=prompt,
            temperature=merged_config.get("temperature", 0.7),
            max_tokens=merged_config.get("max_tokens", 1000),
            top_p=merged_config.get("top_p"),
        )

        content = response.choices[0].message.content
        if content is None:
            raise LLMError("LLM returned empty response")
        return content

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
            LLMError: On API failure or empty response.
        """
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")

        response = await self._execute_completion(
            model=model,
            prompt=prompt,
            temperature=merged_config.get("temperature", 0.7),
            max_tokens=merged_config.get("max_tokens", 1000),
            top_p=merged_config.get("top_p"),
        )

        content = response.choices[0].message.content
        if content is None:
            raise LLMError("LLM returned empty response")

        # Extract usage from response
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        total_tokens = usage.total_tokens if usage else 0

        # Calculate cost
        cost_usd = self._calculate_cost(model, prompt_tokens, completion_tokens, response)

        return LLMResponse(
            content=content,
            usage=LLMUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
            model=model,
            cost_usd=cost_usd,
        )

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
