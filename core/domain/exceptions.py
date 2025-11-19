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


class ConcurrencyError(Exception):
    """Exception raised when optimistic concurrency control detects a conflict.

    This exception indicates that an aggregate was modified by another process
    between when it was loaded and when an attempt was made to save it. This
    is a normal condition in concurrent systems and should trigger a retry
    with a fresh load of the aggregate.

    In event sourcing, this occurs when:
        - Process A loads aggregate at version 5
        - Process B loads aggregate at version 5
        - Process B appends event (version becomes 6)
        - Process A tries to append event expecting version 5
        - ConcurrencyError is raised (actual version is 6, not 5)

    Typical handling:
        1. Catch ConcurrencyError
        2. Reload aggregate from event store (get fresh version)
        3. Replay business logic on fresh state
        4. Retry append with new expected_version

    Attributes:
        aggregate_id: UUID of the aggregate that had the conflict.
        expected_version: The version the process expected.
        actual_version: The actual current version in the database.
    """

    def __init__(
        self,
        aggregate_id: str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        """Initialize ConcurrencyError with conflict details.

        Args:
            aggregate_id: UUID of the aggregate (as string).
            expected_version: The version the process expected.
            actual_version: The actual current version.
        """
        self.aggregate_id = aggregate_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        message = (
            f"Concurrency conflict for aggregate {aggregate_id}: "
            f"expected version {expected_version}, but actual version is {actual_version}"
        )
        super().__init__(message)


class EventStoreError(Exception):
    """Exception raised when event store operations fail.

    This exception wraps infrastructure-level database errors (connection failures,
    query errors, serialization issues) and propagates them to the domain layer.

    Examples of scenarios that raise EventStoreError:
        - Database connection failures
        - SQL query syntax errors
        - JSON serialization/deserialization failures
        - Deadlocks or timeouts
        - Schema errors

    Attributes:
        message: Human-readable error description.
        original_error: The underlying exception from the database layer (optional).
    """

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        """Initialize EventStoreError with message and optional original exception.

        Args:
            message: Human-readable error description.
            original_error: The underlying exception from the database layer.
        """
        self.message = message
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        """Return string representation of the error."""
        if self.original_error:
            return f"{self.message} (caused by: {self.original_error!r})"
        return self.message
