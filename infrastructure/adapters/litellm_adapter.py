"""LiteLLM adapter: unified interface to multiple LLM providers."""

from typing import Any

import litellm

from core.domain.exceptions import LLMError
from core.ports.llm_port import LLMPort


class LiteLLMAdapter(LLMPort):
    """Query LLMs via LiteLLM (OpenAI, Anthropic, etc.)."""

    def __init__(self, default_config: dict[str, Any] | None = None) -> None:
        self.default_config = default_config or {}

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        merged_config = {**self.default_config, **config_dict}
        model = merged_config.get("model", "gpt-4")
        temperature = merged_config.get("temperature", 0.7)
        max_tokens = merged_config.get("max_tokens", 1000)
        top_p = merged_config.get("top_p")

        try:
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
            )

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
                f"LLM service unavailable for model '{model}': {e}", original_error=e
            ) from e

        except Exception as e:
            raise LLMError(
                f"Unexpected LLM error for model '{model}': {e}", original_error=e
            ) from e
