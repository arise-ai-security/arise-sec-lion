"""LiteLLM adapter for unified LLM access.

This adapter provides a unified interface to multiple LLM providers (OpenAI,
Anthropic, etc.) using the LiteLLM library. It implements the LLMPort protocol
from core.ports.
"""

from typing import Any

import litellm

from core.domain.exceptions import LLMError
from core.ports.llm_port import LLMPort


class LiteLLMAdapter(LLMPort):
    """LiteLLM-based adapter for LLM query operations.

    This adapter wraps the LiteLLM library to provide uniform access to
    multiple LLM providers. It handles provider-specific details, retries,
    and error normalization.

    Attributes:
        default_config: Default configuration for LLM queries.
    """

    def __init__(self, default_config: dict[str, Any] | None = None) -> None:
        """Initialize the LiteLLM adapter.

        Args:
            default_config: Default LLM configuration (model, temperature, etc.).
                          Can be overridden per query.
        """
        self.default_config = default_config or {}

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Send a prompt to the LLM and retrieve the response.

        This method merges the provided config_dict with default_config,
        calls the appropriate LLM provider via LiteLLM, and returns the
        text response.

        Args:
            prompt: The text prompt to send to the LLM.
            config_dict: LLM configuration parameters. Common keys:
                - model: str (e.g., "gpt-4", "claude-3-sonnet-20240229")
                - temperature: float (0.0 to 1.0)
                - max_tokens: int
                - top_p: float

        Returns:
            The LLM's text response.

        Raises:
            LLMError: On API failures, rate limits, or invalid configurations.
        """
        # Merge configurations (query config overrides defaults)
        merged_config = {**self.default_config, **config_dict}

        # Extract common parameters
        model = merged_config.get("model", "gpt-4")
        temperature = merged_config.get("temperature", 0.7)
        max_tokens = merged_config.get("max_tokens", 1000)
        top_p = merged_config.get("top_p")

        try:
            # Call LiteLLM with unified interface
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
            )

            # Extract text content from response
            content = response.choices[0].message.content

            if content is None:
                raise LLMError("LLM returned empty response")

            return content

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
            # Catch-all for unexpected errors
            raise LLMError(
                f"Unexpected error querying LLM with model '{model}': {e}",
                original_error=e,
            ) from e
