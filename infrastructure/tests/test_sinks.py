"""Tests for output sinks."""

import io

from core.query.projections.registry import ProjectionRegistry

# All sinks are in infrastructure layer (Hexagonal Architecture)
from infrastructure.adapters.sinks import (
    CallbackSink,
    FileSink,
    MultiSink,
    StderrSink,
    StdoutSink,
    StreamSink,
    StringSink,
)


class TestStdoutSink:
    """Tests for StdoutSink."""

    def test_registered_as_stdout(self) -> None:
        """Should be registered as 'stdout'."""
        sink_cls = ProjectionRegistry.get_sink("stdout")
        assert sink_cls is StdoutSink

    def test_write_prints_to_stdout(self, capsys) -> None:
        """Should print content to stdout."""
        sink = StdoutSink()
        sink.write("Hello, World!")

        captured = capsys.readouterr()
        assert "Hello, World!" in captured.out


class TestStderrSink:
    """Tests for StderrSink."""

    def test_registered_as_stderr(self) -> None:
        """Should be registered as 'stderr'."""
        sink_cls = ProjectionRegistry.get_sink("stderr")
        assert sink_cls is StderrSink

    def test_write_prints_to_stderr(self, capsys) -> None:
        """Should print content to stderr."""
        sink = StderrSink()
        sink.write("Error message")

        captured = capsys.readouterr()
        assert "Error message" in captured.err


class TestFileSink:
    """Tests for FileSink."""

    def test_registered_as_file(self) -> None:
        """Should be registered as 'file'."""
        sink_cls = ProjectionRegistry.get_sink("file")
        assert sink_cls is FileSink

    def test_write_creates_file(self, tmp_path) -> None:
        """Should create file and write content."""
        file_path = tmp_path / "output.txt"
        sink = FileSink(file_path)
        sink.write("Test content")

        assert file_path.exists()
        assert file_path.read_text() == "Test content\n"

    def test_write_overwrites_existing(self, tmp_path) -> None:
        """Should overwrite existing file by default."""
        file_path = tmp_path / "output.txt"
        file_path.write_text("Original")

        sink = FileSink(file_path)
        sink.write("New content")

        assert file_path.read_text() == "New content\n"

    def test_append_mode(self, tmp_path) -> None:
        """Should append when append=True."""
        file_path = tmp_path / "output.txt"
        file_path.write_text("Original\n")

        sink = FileSink(file_path, append=True)
        sink.write("Appended")

        content = file_path.read_text()
        assert "Original" in content
        assert "Appended" in content

    def test_creates_parent_directories(self, tmp_path) -> None:
        """Should create parent directories if they don't exist."""
        file_path = tmp_path / "nested" / "dirs" / "output.txt"
        sink = FileSink(file_path)
        sink.write("Content")

        assert file_path.exists()

    def test_path_property(self, tmp_path) -> None:
        """Should expose path via property."""
        file_path = tmp_path / "output.txt"
        sink = FileSink(file_path)

        assert sink.path == file_path


class TestStringSink:
    """Tests for StringSink."""

    def test_registered_as_string(self) -> None:
        """Should be registered as 'string'."""
        sink_cls = ProjectionRegistry.get_sink("string")
        assert sink_cls is StringSink

    def test_write_captures_content(self) -> None:
        """Should capture content in buffer."""
        sink = StringSink()
        sink.write("First")
        sink.write("Second")

        assert sink.getvalue() == "First\nSecond"

    def test_clear_empties_buffer(self) -> None:
        """Should clear buffer."""
        sink = StringSink()
        sink.write("Content")
        sink.clear()

        assert sink.getvalue() == ""

    def test_lines_property(self) -> None:
        """Should return list of written lines."""
        sink = StringSink()
        sink.write("Line 1")
        sink.write("Line 2")

        assert sink.lines == ["Line 1", "Line 2"]


class TestCallbackSink:
    """Tests for CallbackSink."""

    def test_registered_as_callback(self) -> None:
        """Should be registered as 'callback'."""
        sink_cls = ProjectionRegistry.get_sink("callback")
        assert sink_cls is CallbackSink

    def test_write_calls_callback(self) -> None:
        """Should call callback with content."""
        collected = []
        sink = CallbackSink(collected.append)
        sink.write("Test")

        assert collected == ["Test"]

    def test_multiple_writes(self) -> None:
        """Should call callback for each write."""
        collected = []
        sink = CallbackSink(collected.append)
        sink.write("A")
        sink.write("B")
        sink.write("C")

        assert collected == ["A", "B", "C"]


class TestMultiSink:
    """Tests for MultiSink."""

    def test_writes_to_all_sinks(self) -> None:
        """Should write to all child sinks."""
        sink1 = StringSink()
        sink2 = StringSink()
        multi = MultiSink([sink1, sink2])

        multi.write("Content")

        assert sink1.getvalue() == "Content"
        assert sink2.getvalue() == "Content"

    def test_add_sink(self) -> None:
        """Should allow adding sinks after creation."""
        sink1 = StringSink()
        multi = MultiSink([sink1])

        sink2 = StringSink()
        multi.add(sink2)

        multi.write("Content")

        assert sink1.getvalue() == "Content"
        assert sink2.getvalue() == "Content"

    def test_empty_multi_sink(self) -> None:
        """Should handle empty sink list."""
        multi = MultiSink([])
        multi.write("Content")  # Should not raise


class TestStreamSink:
    """Tests for StreamSink."""

    def test_write_to_string_io(self) -> None:
        """Should write to StringIO stream."""
        buffer = io.StringIO()
        sink = StreamSink(buffer)
        sink.write("Content")

        assert buffer.getvalue() == "Content\n"

    def test_no_newline_option(self) -> None:
        """Should skip newline when newline=False."""
        buffer = io.StringIO()
        sink = StreamSink(buffer, newline=False)
        sink.write("Content")

        assert buffer.getvalue() == "Content"

    def test_preserves_existing_newline(self) -> None:
        """Should not double newline if content already ends with one."""
        buffer = io.StringIO()
        sink = StreamSink(buffer)
        sink.write("Content\n")

        assert buffer.getvalue() == "Content\n"
