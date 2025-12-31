"""LLM port: query interface for LLM interactions."""

from typing import Any, Protocol

from core.domain.values.llm_response import LLMResponse


class LLMPort(Protocol):
    """Query LLM with prompt and config (model, temperature, max_tokens, etc.)."""

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Send prompt, return response text. Raises LLMError on failure.

        This is the simple interface for cases where cost tracking is not needed.
        """
        ...

    async def query_with_usage(self, prompt: str, config_dict: dict[str, Any]) -> LLMResponse:
        """Send prompt, return response with token usage and cost metadata.

        This method enables cost tracking by returning the full LLMResponse
        including token counts and calculated cost.

        Args:
            prompt: The prompt to send to the LLM.
            config_dict: Configuration including model, temperature, max_tokens.

        Returns:
            LLMResponse containing content, usage, model, and cost_usd.

        Raises:
            LLMError: On API failure or timeout.
        """
        ...
