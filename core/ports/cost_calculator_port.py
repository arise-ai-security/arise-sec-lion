"""Port for calculating LLM and worker tool costs.

This port abstracts cost calculation, enabling different pricing
strategies (static tables, API-based, custom) without coupling
the domain to specific implementations.
"""

from typing import Protocol


class CostCalculatorPort(Protocol):
    """Calculate USD cost from token usage and model information.

    Implementations provide pricing logic for LLM calls and worker tools.
    The domain uses this port to calculate costs without knowing specific
    pricing details.
    """

    def calculate_llm_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Calculate cost in USD for an LLM call.

        Args:
            model: Name of the model (e.g., "gpt-4o", "claude-3-5-sonnet-20241022").
            prompt_tokens: Number of input tokens.
            completion_tokens: Number of output tokens.

        Returns:
            Cost in USD, rounded to 6 decimal places.
        """
        ...

    def calculate_worker_cost(
        self,
        tool_name: str,
        model: str | None,
        tokens: int | None,
        duration_seconds: float,
    ) -> float:
        """Calculate estimated cost in USD for worker tool execution.

        Worker tools like Claude Code or OpenHands may have different
        pricing models. Some are token-based, others time-based.

        Args:
            tool_name: Worker tool identifier ("claude_code", "openhands").
            model: Underlying model if known (may be None for PTY tools).
            tokens: Total tokens if available (may be None).
            duration_seconds: Execution duration in seconds.

        Returns:
            Estimated cost in USD, rounded to 6 decimal places.
        """
        ...
