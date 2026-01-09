"""Domain exceptions."""


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

    In standard OCC, the caller reloads the aggregate on conflict,
    so actual_version is optional (caller discovers it on reload).
    """

    def __init__(
        self,
        aggregate_id: str,
        expected_version: int,
        actual_version: int | None = None,
    ) -> None:
        self.aggregate_id = aggregate_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        if actual_version is not None:
            msg = f"Concurrency conflict for {aggregate_id}: expected v{expected_version}, actual v{actual_version}"
        else:
            msg = f"Concurrency conflict for {aggregate_id}: expected v{expected_version}"
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


class CostInvariantViolation(Exception):
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
            f"Cost invariant violated: {invariant}\n"
            f"  Expected: {expected}\n"
            f"  Actual: {actual}"
        )
        if context:
            message += f"\n  Context: {context}"

        super().__init__(message)


class ResearchError(Exception):
    """Research operation failed (tool execution, context gathering).

    Raised by ResearchPort implementations when research tool calling
    encounters non-recoverable errors.
    """

    def __init__(self, message: str, original_error: Exception | None = None) -> None:
        self.message = message
        self.original_error = original_error
        super().__init__(message)

    def __str__(self) -> str:
        if self.original_error:
            return f"{self.message} (caused by: {self.original_error!r})"
        return self.message


class InvalidRoleTransitionError(Exception):
    """Invalid role transition attempted on an agent.

    Raised when attempting a role transition that violates
    the agent state machine (e.g., WORKER -> MANAGER).
    """

    def __init__(
        self,
        current_role: str,
        target_role: str,
        reason: str = "",
    ) -> None:
        self.current_role = current_role
        self.target_role = target_role
        self.reason = reason
        message = f"Cannot transition from {current_role} to {target_role}"
        if reason:
            message += f": {reason}"
        super().__init__(message)
