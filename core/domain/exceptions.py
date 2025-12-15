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
    """Optimistic concurrency conflict - aggregate modified by another process."""

    def __init__(
        self,
        aggregate_id: str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.aggregate_id = aggregate_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Concurrency conflict for {aggregate_id}: "
            f"expected v{expected_version}, actual v{actual_version}"
        )


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
