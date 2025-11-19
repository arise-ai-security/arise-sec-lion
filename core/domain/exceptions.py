"""Domain exceptions for the multi-agent system.

This module defines domain-specific exceptions that represent business failures
or infrastructure errors that impact domain operations. These exceptions cross
the hexagonal architecture boundary from infrastructure to domain.
"""


class LLMError(Exception):
    """Exception raised when LLM operations fail.

    This exception wraps infrastructure-level LLM errors (API failures, rate limits,
    invalid configurations, etc.) and propagates them to the domain layer.

    Examples of scenarios that raise LLMError:
        - API authentication failures
        - Rate limit exceeded
        - Invalid model name
        - Network timeouts
        - Malformed API responses
        - Provider service outages

    Attributes:
        message: Human-readable error description.
        original_error: The underlying exception from the infrastructure layer (optional).
    """

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        """Initialize LLMError with message and optional original exception.

        Args:
            message: Human-readable error description.
            original_error: The underlying exception from the infrastructure layer.
        """
        self.message = message
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.original_error:
            return f"{self.message} (caused by: {self.original_error!r})"
        return self.message
