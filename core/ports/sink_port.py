"""Sink port: output destination (stdout, file, buffer)."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SinkPort(Protocol):
    """Write formatted output to destination."""

    def write(self, content: str) -> None:
        """Write content string to destination."""
        ...
