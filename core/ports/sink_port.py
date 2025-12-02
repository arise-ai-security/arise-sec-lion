"""Sink port definition for output destinations.

This module defines the abstract interface for output sinks.
Infrastructure adapters (e.g., StdoutSink, FileSink) must implement this protocol.

Following Hexagonal Architecture:
- This PORT lives in core/ports/ (abstraction)
- ADAPTERS live in infrastructure/adapters/ (implementations)
- Application layer depends only on this abstraction
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SinkPort(Protocol):
    """Abstract interface for output destinations.

    Sinks receive formatted strings and write them to their destination.
    Implementations handle the actual I/O operations and live in the
    infrastructure layer.

    Example implementations (in infrastructure/adapters/):
        - StdoutSink: Prints to standard output
        - StderrSink: Prints to standard error
        - FileSink: Writes to a file

    Example test doubles (no real I/O):
        - StringSink: Captures to an in-memory buffer
        - CallbackSink: Calls a user-provided function
    """

    def write(self, content: str) -> None:
        """Write formatted content to the destination.

        Args:
            content: The formatted string to write.
        """
        ...
