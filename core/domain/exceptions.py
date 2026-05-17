"""Domain exceptions."""

from core.domain.values.constraint_failure import ConstraintFailure


class DomainInvariantError(ValueError):
    """A domain aggregate or event violated an enforced invariant."""


class InvalidEventHistoryError(ValueError):
    """Event history is malformed and cannot be replayed safely."""


class LLMError(Exception):
    """LLM operation failed (API error, rate limit, timeout)."""

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.message = message
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        if self.original_error:
            return f"{self.message} (caused by: {self.original_error!r})"
        return self.message


class ConcurrencyError(Exception):
    """Optimistic concurrency conflict - aggregate modified by another process.

    OCC is enforced by the UNIQUE(aggregate_id, sequence_number) constraint
    on the event store. On conflict, the caller reloads the aggregate and
    retries. The optional ``actual_version`` is the count of events the
    losing writer observed (discovered on reload).
    """

    def __init__(
        self,
        aggregate_id: str,
        actual_version: int | None = None,
    ) -> None:
        self.aggregate_id = aggregate_id
        self.actual_version = actual_version
        if actual_version is not None:
            msg = (
                f"Concurrency conflict for {aggregate_id}: "
                f"actual v{actual_version}"
            )
        else:
            msg = f"Concurrency conflict for {aggregate_id}"
        super().__init__(msg)


class EventStoreError(Exception):
    """Event store operation failed (DB connection, query, serialization)."""

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.message = message
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        if self.original_error:
            return f"{self.message} (caused by: {self.original_error!r})"
        return self.message


class ToolNotAvailableError(Exception):
    """Requested worker tool not configured or unavailable."""

    def __init__(self, tool_name: str, available_tools: list[str]) -> None:
        self.tool_name = tool_name
        self.available_tools = available_tools
        super().__init__(f"Tool '{tool_name}' unavailable. Available: {available_tools}")


class InfeasibleError(ValueError):
    """LLM reported that the task is infeasible under current constraints."""

    def __init__(self, failure: ConstraintFailure) -> None:
        self.failure = failure
        super().__init__(failure.format_message())


class CostInvariantViolation(Exception):  # noqa: N818
    """Raised when a cost invariant is violated.

    This indicates a bug in the cost calculation logic that must be fixed.
    These errors should never occur in production if tests pass.
    """

    def __init__(
        self,
        invariant: str,
        expected: object,
        actual: object,
        context: dict[str, object] | None = None,
    ) -> None:
        self.invariant = invariant
        self.expected = expected
        self.actual = actual
        self.context = context or {}

        message = (
            f"Cost invariant violated: {invariant}\n  Expected: {expected}\n  Actual: {actual}"
        )
        if context:
            message += f"\n  Context: {context}"

        super().__init__(message)
