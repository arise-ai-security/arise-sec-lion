"""LLM port definition.

This module defines the abstract interface for Large Language Model interactions.
Infrastructure adapters (e.g., LiteLLM) must implement this protocol.
"""

from typing import Any, Protocol


class LLMPort(Protocol):
    """Abstract interface for LLM query operations.

    This port abstracts LLM interactions for BOSS and MANAGER agents to
    decompose tasks, plan, and make decisions. Infrastructure implementations
    (e.g., LiteLLM adapter) handle provider-specific details (OpenAI, Anthropic, etc.).

    Implementations should support:
    - Multiple LLM providers via unified interface
    - Configurable parameters (temperature, max_tokens, etc.)
    - Error handling and retries
    """

    async def query(self, prompt: str, config_dict: dict[str, Any]) -> str:
        """Send a prompt to the LLM and retrieve the response.

        Args:
            prompt: The text prompt to send to the LLM.
            config_dict: LLM configuration parameters. Common keys include:
                - model: str (e.g., "gpt-4", "claude-3-sonnet")
                - temperature: float (0.0 to 1.0)
                - max_tokens: int
                - top_p: float
                - stream: bool (for future streaming support)

        Returns:
            The LLM's text response.

        Raises:
            LLMError: On API failures, rate limits, or invalid configurations.
            ValidationError: If config_dict contains invalid parameters.
        """
        ...
