"""LLM port: query interface for LLM interactions."""

from typing import Any, Protocol


class LLMPort(Protocol):
    """Query LLM with prompt and config (model, temperature, max_tokens, etc.)."""

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Send prompt, return response text. Raises LLMError on failure."""
        ...
