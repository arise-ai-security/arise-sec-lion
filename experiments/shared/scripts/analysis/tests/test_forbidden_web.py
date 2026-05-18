"""Unit tests for forbidden-web detection (design doc §9 / §13.3 matrix)."""

from __future__ import annotations

from uuid import uuid4

from experiments.shared.scripts.analysis.tests.factories import (
    event_row,
    tool_use_event,
)
from experiments.shared.scripts.analysis.text.forbidden_web import detect_violations


class TestDirectToolViolations:
    def test_webfetch_tool_use_yields_direct_tool_violation(self) -> None:
        # Given: a ``WebFetch`` tool_use event on the A1/A2 path (no
        # structured field — tool name is recovered from the ``Tool: ``
        # fallthrough prefix).
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="WebFetch",
                tool_input={"url": "https://example.com"},
                set_structured_field=False,
            )
        ]

        # When
        violations = detect_violations(events)

        # Then: exactly one violation, tagged as a direct-tool invocation
        assert len(violations) == 1
        v = violations[0]
        assert v.via == "direct_tool"
        assert v.tool_or_cmd == "WebFetch"
        assert v.aggregate_id == agg
        assert v.sequence_number == 1

    def test_websearch_tool_use_yields_direct_tool_violation(self) -> None:
        # Given: a ``WebSearch`` tool_use event
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=2,
                tool_name="WebSearch",
                tool_input={"query": "secrets"},
                set_structured_field=False,
            )
        ]

        # When
        violations = detect_violations(events)

        # Then
        assert len(violations) == 1
        assert violations[0].via == "direct_tool"
        assert violations[0].tool_or_cmd == "WebSearch"


class TestBashCommandViolations:
    def test_curl_command_yields_bash_violation_with_url_evidence(self) -> None:
        # Given: a Bash ``curl`` invocation against an https URL
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=10,
                tool_name="Bash",
                tool_input={"command": "curl https://example.com"},
            )
        ]

        # When
        violations = detect_violations(events)

        # Then: 1 bash_command violation; URL preserved in evidence
        assert len(violations) == 1
        v = violations[0]
        assert v.via == "bash_command"
        assert "curl" in v.tool_or_cmd
        assert "https://example.com" in v.evidence_snippet

    def test_wget_command_yields_bash_violation(self) -> None:
        # Given
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=3,
                tool_name="Bash",
                tool_input={"command": "wget -O /tmp/x http://y"},
            )
        ]

        # When
        violations = detect_violations(events)

        # Then
        assert len(violations) == 1
        assert violations[0].via == "bash_command"

    def test_netcat_listener_yields_bash_violation(self) -> None:
        # Given: ``nc -lvnp 4444`` — matches ``nc(?:at)?\s+-``
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=4,
                tool_name="Bash",
                tool_input={"command": "nc -lvnp 4444"},
            )
        ]

        # When
        violations = detect_violations(events)

        # Then
        assert len(violations) == 1
        assert violations[0].via == "bash_command"

    def test_python_requests_oneliner_yields_bash_violation(self) -> None:
        # Given: a python -c one-liner importing requests + calling get()
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=5,
                tool_name="Bash",
                tool_input={
                    "command": "python3 -c \"import requests; requests.get('https://z')\"",
                },
            )
        ]

        # When
        violations = detect_violations(events)

        # Then
        assert len(violations) == 1
        assert violations[0].via == "bash_command"


class TestNegativeCases:
    def test_innocuous_ls_yields_no_violation(self) -> None:
        # Given: ``ls -la`` — no web egress signal
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="Bash",
                tool_input={"command": "ls -la"},
            )
        ]

        # When
        violations = detect_violations(events)

        # Then
        assert violations == []

    def test_gcc_build_yields_no_violation(self) -> None:
        # Given: a local C build command
        agg = uuid4()
        events = [
            tool_use_event(
                agg,
                seq=1,
                tool_name="Bash",
                tool_input={"command": "gcc -o foo foo.c"},
            )
        ]

        # When
        violations = detect_violations(events)

        # Then
        assert violations == []

    def test_non_tool_use_thoughtcaptured_is_ignored(self) -> None:
        # Given: a ThoughtCaptured row with ``output_type='output'`` — not
        # a tool_use event, so detection must skip it entirely. The factory
        # ``tool_use_event`` hardcodes output_type='tool_use', so build the
        # row directly via ``event_row``.
        agg = uuid4()
        events = [
            event_row(
                agg,
                seq=1,
                event_type="ThoughtCaptured",
                payload={
                    "output_type": "output",
                    "content": "curl https://example.com",
                    "tool_name": None,
                    "stream": "claude_code",
                },
            )
        ]

        # When
        violations = detect_violations(events)

        # Then: the URL in content is irrelevant — only tool_use rows count
        assert violations == []

    def test_empty_event_list_returns_empty(self) -> None:
        # Given: no events at all
        # When
        violations = detect_violations([])

        # Then
        assert violations == []
