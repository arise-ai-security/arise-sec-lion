"""Sink adapters: stdout, stderr, file, string, callback."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from core.application.projections.registry import register_sink
from core.ports.sink_port import SinkPort


@register_sink("stdout")
class StdoutSink:
    """Print to stdout."""

    def write(self, content: str) -> None:
        print(content)


@register_sink("stderr")
class StderrSink:
    """Print to stderr."""

    def write(self, content: str) -> None:
        print(content, file=sys.stderr)


@register_sink("file")
class FileSink:
    """Write to file."""

    def __init__(self, path: Path | str, encoding: str = "utf-8", append: bool = False) -> None:
        self._path = Path(path)
        self._encoding = encoding
        self._append = append

    @property
    def path(self) -> Path:
        return self._path

    def write(self, content: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self._append else "w"
        with self._path.open(mode, encoding=self._encoding) as f:
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")


@register_sink("string")
class StringSink:
    """Capture to in-memory buffer (for testing)."""

    def __init__(self) -> None:
        self._buffer: list[str] = []

    def write(self, content: str) -> None:
        self._buffer.append(content)

    def getvalue(self) -> str:
        return "\n".join(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    @property
    def lines(self) -> list[str]:
        return list(self._buffer)


@register_sink("callback")
class CallbackSink:
    """Invoke callback on each write."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def write(self, content: str) -> None:
        self._callback(content)


class MultiSink:
    """Write to multiple sinks."""

    def __init__(self, sinks: list[SinkPort]) -> None:
        self._sinks = list(sinks)

    def write(self, content: str) -> None:
        for sink in self._sinks:
            sink.write(content)

    def add(self, sink: SinkPort) -> None:
        self._sinks.append(sink)


class StreamSink:
    """Write to TextIO stream."""

    def __init__(self, stream: TextIO, newline: bool = True) -> None:
        self._stream = stream
        self._newline = newline

    def write(self, content: str) -> None:
        self._stream.write(content)
        if self._newline and not content.endswith("\n"):
            self._stream.write("\n")
