"""Unit tests for ``recover_tool_name`` (design doc §7)."""

from __future__ import annotations

from experiments.shared.scripts.analysis.text.prefixes import recover_tool_name


class TestKnownPrefixes:
    def test_running_prefix_recovers_bash(self) -> None:
        # Given: a content blob shaped like ``format_tool_event`` for Bash
        content = 'Running: list files\nInput: {"command": "ls -la"}'

        # When: recover with no structured field (A1/A2 path)
        name = recover_tool_name(content, None)

        # Then: Bash is recovered from the "Running: " prefix
        assert name == "Bash"

    def test_reading_prefix_recovers_read(self) -> None:
        # Given: a Read tool_use rendering
        content = "Reading: /tmp/foo.txt\nInput: {}"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "Read"

    def test_writing_prefix_recovers_write(self) -> None:
        # Given
        content = "Writing: /tmp/out.txt\nInput: {}"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "Write"

    def test_editing_prefix_recovers_edit(self) -> None:
        # Given
        content = "Editing: /tmp/file.py\nInput: {}"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "Edit"

    def test_searching_files_prefix_recovers_glob(self) -> None:
        # Given
        content = "Searching files: **/*.py\nInput: {}"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "Glob"

    def test_searching_content_prefix_recovers_grep(self) -> None:
        # Given
        content = "Searching content: foo\nInput: {}"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "Grep"


class TestFallthroughPrefix:
    def test_tool_fallthrough_with_following_input(self) -> None:
        # Given: a "Tool: Task" rendering with the Input marker on a new line
        content = 'Tool: Task\nInput: {"description": "spawn worker"}'

        # When
        name = recover_tool_name(content, None)

        # Then: the literal token after "Tool: " is returned
        assert name == "Task"

    def test_tool_fallthrough_taskcreate(self) -> None:
        # Given
        content = "Tool: TaskCreate"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "TaskCreate"

    def test_tool_fallthrough_webfetch(self) -> None:
        # Given: the direct-web tool path emits a "Tool: WebFetch" header
        content = 'Tool: WebFetch\nInput: {"url": "https://x"}'

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "WebFetch"

    def test_tool_fallthrough_preserves_mcp_literal(self) -> None:
        # Given: MCP-style tool names contain underscores; the regex
        # captures the full \S+ token verbatim.
        content = "Tool: mcp__security_tools__shell_in_container\nInput: {}"

        # When
        name = recover_tool_name(content, None)

        # Then: the full namespaced literal is preserved
        assert name == "mcp__security_tools__shell_in_container"


class TestStructuredFieldShortCircuit:
    def test_non_empty_field_overrides_content(self) -> None:
        # Given: a content blob that would resolve to "Bash" by prefix,
        # but the structured field disagrees (post-bugfix B1 path).
        content = 'Running: irrelevant\nInput: {"command": "ls"}'

        # When: structured field is supplied
        name = recover_tool_name(content, "WebFetch")

        # Then: the structured field wins (content is not consulted)
        assert name == "WebFetch"

    def test_empty_content_with_structured_field_returns_field(self) -> None:
        # Given: empty content but a non-empty structured field
        # When
        name = recover_tool_name("", "Read")

        # Then
        assert name == "Read"


class TestFallbacks:
    def test_empty_content_and_none_field_returns_unknown(self) -> None:
        # Given: no content, no structured field
        # When
        name = recover_tool_name("", None)

        # Then: never crashes; always counted under "unknown"
        assert name == "unknown"

    def test_malformed_content_returns_unknown(self) -> None:
        # Given: a content blob that matches neither the prefix table
        # nor the ``Tool: <name>`` fallthrough
        content = "garbage prefix that should not match anything"

        # When
        name = recover_tool_name(content, None)

        # Then
        assert name == "unknown"

    def test_empty_string_structured_field_is_treated_as_falsy(self) -> None:
        # Given: an empty-string structured field (truthy precedence in §7
        # requires "non-empty") plus content that recovers via prefix
        content = "Running: ls\nInput: {}"

        # When
        name = recover_tool_name(content, "")

        # Then: empty string is falsy; prefix recovery proceeds
        assert name == "Bash"
