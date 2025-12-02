"""Concrete sink adapters for output destinations.

This module provides infrastructure implementations of the SinkPort protocol.
These adapters handle I/O operations and output destinations.

Following Hexagonal Architecture:
- These ADAPTERS live in infrastructure/adapters/
- They implement the SinkPort from core/ports/
- Application layer receives these via dependency injection
- Registration happens at import time (plugin pattern)

Available sinks:
    - "stdout": StdoutSink - Prints to standard output
    - "stderr": StderrSink - Prints to standard error
    - "file": FileSink - Writes to a file
    - "string": StringSink - Captures to in-memory buffer (testing)
    - "callback": CallbackSink - Calls a user-provided function
    - MultiSink - Writes to multiple sinks (composition, not registered)
    - StreamSink - Writes to any TextIO stream (not registered)
"""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from core.application.projections.registry import register_sink
from core.ports.sink_port import SinkPort


@register_sink("stdout")
class StdoutSink:
    """Sink that writes to standard output.

    Simple sink that prints content to stdout with a trailing newline.
    Useful for CLI tools and interactive output.
    """

    def write(self, content: str) -> None:
        """Print content to stdout.

        Args:
            content: The formatted string to output.
        """
        print(content)


@register_sink("stderr")
class StderrSink:
    """Sink that writes to standard error.

    Useful for error reports and diagnostics that should be
    separate from normal output.
    """

    def write(self, content: str) -> None:
        """Print content to stderr.

        Args:
            content: The formatted string to output.
        """
        print(content, file=sys.stderr)


@register_sink("file")
class FileSink:
    """Sink that writes to a file.

    Writes content to a file, overwriting any existing content by default.
    Creates parent directories if they don't exist.

    Attributes:
        path: The file path to write to.
        encoding: Character encoding (default: utf-8).
    """

    def __init__(
        self,
        path: Path | str,
        encoding: str = "utf-8",
        append: bool = False,
    ) -> None:
        """Initialize with file path.

        Args:
            path: Path to the output file.
            encoding: Character encoding for the file.
            append: If True, append to existing file. Default is False (overwrite).
        """
        self._path = Path(path)
        self._encoding = encoding
        self._append = append

    @property
    def path(self) -> Path:
        """Get the file path.

        Returns:
            The Path object for the output file.
        """
        return self._path

    def write(self, content: str) -> None:
        """Write content to the file.

        Creates parent directories if needed.

        Args:
            content: The formatted string to write.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self._append else "w"
        with self._path.open(mode, encoding=self._encoding) as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")


@register_sink("string")
class StringSink:
    """Sink that captures output to an in-memory buffer.

    Ideal for testing and scenarios where you need to capture
    output for further processing.

    Usage:
        sink = StringSink()
        sink.write("hello")
        sink.write("world")
        assert sink.getvalue() == "hello\\nworld"
    """

    def __init__(self) -> None:
        """Initialize with an empty buffer."""
        self._buffer: list[str] = []

    def write(self, content: str) -> None:
        """Append content to the buffer.

        Args:
            content: The formatted string to capture.
        """
        self._buffer.append(content)

    def getvalue(self) -> str:
        """Get all captured content.

        Returns:
            All written content joined with newlines.
        """
        return "\n".join(self._buffer)

    def clear(self) -> None:
        """Clear the buffer."""
        self._buffer.clear()

    @property
    def lines(self) -> list[str]:
        """Get captured content as a list of lines.

        Returns:
            List of strings that were written.
        """
        return list(self._buffer)


@register_sink("callback")
class CallbackSink:
    """Sink that calls a user-provided function.

    Useful for integrating with custom logging systems,
    message queues, or other output handlers.

    Usage:
        collected = []
        sink = CallbackSink(collected.append)
        sink.write("hello")
        assert collected == ["hello"]
    """

    def __init__(self, callback: Callable[[str], None]) -> None:
        """Initialize with a callback function.

        Args:
            callback: Function to call with each write.
        """
        self._callback = callback

    def write(self, content: str) -> None:
        """Call the callback with the content.

        Args:
            content: The formatted string to pass to the callback.
        """
        self._callback(content)


class MultiSink:
    """Sink that writes to multiple destinations.

    Composition pattern for writing the same output to multiple sinks.

    Usage:
        sink = MultiSink([
            StdoutSink(),
            FileSink("output.log"),
        ])
        sink.write("hello")  # Writes to both stdout and file
    """

    def __init__(self, sinks: list[SinkPort]) -> None:
        """Initialize with a list of sinks.

        Args:
            sinks: List of sink instances to write to.
        """
        self._sinks = list(sinks)

    def write(self, content: str) -> None:
        """Write content to all sinks.

        Args:
            content: The formatted string to write.
        """
        for sink in self._sinks:
            sink.write(content)

    def add(self, sink: SinkPort) -> None:
        """Add a sink to the list.

        Args:
            sink: Sink instance to add.
        """
        self._sinks.append(sink)


class StreamSink:
    """Sink that writes to any file-like object.

    Low-level bridge for writing to arbitrary streams.

    Usage:
        import io
        buffer = io.StringIO()
        sink = StreamSink(buffer)
        sink.write("hello")
        assert buffer.getvalue() == "hello\\n"
    """

    def __init__(self, stream: TextIO, newline: bool = True) -> None:
        """Initialize with a stream.

        Args:
            stream: File-like object with a write method.
            newline: If True, append newline after content.
        """
        self._stream = stream
        self._newline = newline

    def write(self, content: str) -> None:
        """Write content to the stream.

        Args:
            content: The formatted string to write.
        """
        self._stream.write(content)
        if self._newline and not content.endswith("\n"):
            self._stream.write("\n")
