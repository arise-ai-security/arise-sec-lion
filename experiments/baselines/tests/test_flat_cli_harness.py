"""Tests for the flat Claude Code CLI harness stream parser + helpers."""

from __future__ import annotations

import asyncio
import io
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from experiments.baselines.flat_cli_harness import (
    CELL_CONFIGS,
    RunSpec,
    _stream_to_events,
    build_prompt,
    compute_redundancy_targets,
    parse_stream_line,
)
from experiments.schema import (
    TokensConsumedPayload,
    ToolResultPayload,
    ToolUsePayload,
)


def test_parse_stream_line_tool_use_block() -> None:
    """assistant tool_use becomes a tool_use NormalizedEvent with structured input."""
    # Given: a stream-json line containing an assistant message with a tool_use block
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "Bash",
                        "input": {"command": "ls /src"},
                    },
                ],
            },
        }
    )
    run_id = "test-run"

    # When: parse_stream_line is called
    events = parse_stream_line(line, run_id=run_id, now=datetime.now(UTC))

    # Then: a single NormalizedEvent of type "tool_use" with full structured input
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "tool_use"
    assert isinstance(ev.payload, ToolUsePayload)
    assert ev.payload.tool_name == "Bash"
    assert ev.payload.tool_input == {"command": "ls /src"}
    assert ev.payload.call_id == "toolu_abc"
    assert ev.source == "flat_cli"
    assert ev.role == "FLAT"


def test_parse_stream_line_tool_result_block() -> None:
    """user tool_result becomes a tool_result NormalizedEvent preserving call_id."""
    # Given: a user message containing a tool_result block
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "file1\nfile2\n",
                    },
                ],
            },
        }
    )

    # When: parsed
    events = parse_stream_line(line, run_id="r", now=datetime.now(UTC))

    # Then: one tool_result with the right call_id and full text
    assert len(events) == 1
    assert events[0].event_type == "tool_result"
    assert isinstance(events[0].payload, ToolResultPayload)
    assert events[0].payload.call_id == "toolu_abc"
    assert events[0].payload.result_text == "file1\nfile2\n"


def test_parse_stream_line_result_message_emits_tokens_consumed() -> None:
    """final result message emits tokens_consumed and run_completed events."""
    # Given: the final result message with cost + usage
    line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "total_cost_usd": 0.0234,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 400,
                "cache_creation_input_tokens": 200,
            },
            "num_turns": 3,
            "duration_ms": 30000,
        }
    )

    # When: parsed
    events = parse_stream_line(line, run_id="r", now=datetime.now(UTC))

    # Then: one tokens_consumed + one run_completed
    types = [e.event_type for e in events]
    assert "tokens_consumed" in types
    assert "run_completed" in types
    tc = next(e for e in events if e.event_type == "tokens_consumed")
    assert isinstance(tc.payload, TokensConsumedPayload)
    assert tc.payload.input_tokens == 100
    assert tc.payload.cache_read_input_tokens == 400
    assert tc.payload.cost_usd == 0.0234


def test_parse_stream_line_text_assistant_message() -> None:
    """plain text assistant messages parse without raising and emit nothing."""
    # Given: a plain text assistant message (no tool use)
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Analyzing the code..."}],
            },
        }
    )

    # When: parsed
    events = parse_stream_line(line, run_id="r", now=datetime.now(UTC))

    # Then: empty list (text-only assistant messages don't become events in flat arm)
    assert isinstance(events, list)
    assert events == []


def test_parse_stream_line_unknown_type_returns_empty() -> None:
    """unknown message types return [] without raising."""
    # Given: a line with a type we don't recognize
    line = json.dumps({"type": "system", "subtype": "init"})

    # When: parsed
    events = parse_stream_line(line, run_id="r", now=datetime.now(UTC))

    # Then: empty list, no exception
    assert events == []


def test_parse_stream_line_invalid_json_returns_empty_and_logs() -> None:
    """malformed JSON returns [] and does not raise."""
    # Given: a malformed line
    line = "{this is not json"

    # When: parsed
    events = parse_stream_line(line, run_id="r", now=datetime.now(UTC))

    # Then: empty list, no exception (error logged)
    assert events == []


def test_parse_stream_line_tool_result_truncates_large_payloads() -> None:
    """very large tool_result payloads are capped to MAX_RESULT_BYTES with was_truncated=True."""
    # Given: a tool_result with > 10K bytes of content
    huge = "X" * 20_000
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": huge},
                ],
            },
        }
    )

    # When: parsed
    events = parse_stream_line(line, run_id="r", now=datetime.now(UTC))

    # Then: result_text is capped, was_truncated is True, result_bytes is the original size
    assert len(events) == 1
    p = events[0].payload
    assert isinstance(p, ToolResultPayload)
    assert p.was_truncated is True
    assert p.result_bytes == 20_000
    assert len(p.result_text) <= 10_240


def test_compute_redundancy_targets_normalizes_known_tools() -> None:
    """Redundancy targets normalize Read/Edit (file_path), Bash (cmd+args), Grep/Glob (pattern)."""
    # Given/When/Then: each tool maps to its canonical target string
    assert compute_redundancy_targets("Read", {"file_path": "/src/foo.c"}) == "/src/foo.c"
    assert compute_redundancy_targets("Edit", {"file_path": "/src/foo.c"}) == "/src/foo.c"
    assert compute_redundancy_targets("Bash", {"command": "git status -uno"}) == "git status -uno"
    assert compute_redundancy_targets("Grep", {"pattern": "TODO"}) == "TODO"
    assert compute_redundancy_targets("Glob", {"pattern": "**/*.c"}) == "**/*.c"


def test_build_prompt_a3_includes_preamble_briefing_task_in_order() -> None:
    """Cell A3 prompt is preamble + briefing + task in order."""
    # Given: cell A3 (subagents on, briefing on)
    cell = CELL_CONFIGS["A3"]
    task = "Reproduce CVE-2022-32414."

    # When: prompt composed
    prompt = build_prompt(task, cell)

    # Then: contains preamble + briefing + task in that order
    assert "AVAILABLE SECURITY TOOLS" in prompt
    assert "SEC-bench Task Context" in prompt  # from briefing doc
    assert "Reproduce CVE-2022-32414." in prompt
    assert prompt.index("AVAILABLE SECURITY TOOLS") < prompt.index("SEC-bench Task Context")
    assert prompt.index("SEC-bench Task Context") < prompt.index("Reproduce CVE-2022-32414.")


def test_build_prompt_a1_omits_briefing() -> None:
    """Cell A1 prompt has preamble + task but no briefing."""
    # Given: cell A1 (subagents on, briefing off)
    cell = CELL_CONFIGS["A1"]

    # When: prompt composed
    prompt = build_prompt("task", cell)

    # Then: preamble present, briefing absent
    assert "AVAILABLE SECURITY TOOLS" in prompt
    assert "SEC-bench Task Context" not in prompt


def _make_fake_proc(lines: list[str]) -> MagicMock:
    """Fake asyncio subprocess with a StreamReader pre-seeded with ``lines``."""
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line.encode("utf-8"))
    reader.feed_eof()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    proc.kill = MagicMock()
    return proc


def _make_spec(budget_usd_cap: float = 3.0, wallclock_sec_cap: int = 600) -> RunSpec:
    return RunSpec(
        cve_instance_path=Path("unused.json"),
        cell=CELL_CONFIGS["A1"],
        replicate=1,
        output_dir=Path("unused"),
        docker_image="unused",
        model="claude-sonnet-4-6",
        budget_usd_cap=budget_usd_cap,
        wallclock_sec_cap=wallclock_sec_cap,
        workspace_host_root=Path("unused"),
    )


@pytest.mark.asyncio
async def test_stream_to_events_returns_completed_on_eof() -> None:
    """Clean stream exhaustion yields termination_reason='completed'."""
    # Given: a fake proc emitting one tool_use line then EOF
    tool_line = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    proc = _make_fake_proc([tool_line])
    events_f = io.StringIO()
    log_f = io.StringIO()

    # When: _stream_to_events drains the stream
    reason, last_seq, total_cost = await _stream_to_events(
        proc,
        events_f,
        log_f,
        run_id="r",
        spec=_make_spec(),
        started_monotonic=time.monotonic(),
        starting_seq=0,
    )

    # Then: reason is "completed", one event was emitted, no kill issued
    assert reason == "completed"
    assert last_seq == 1
    assert total_cost == 0.0
    proc.kill.assert_not_called()
    assert events_f.getvalue().count("\n") == 1


@pytest.mark.asyncio
async def test_stream_to_events_triggers_budget_cap_and_kills() -> None:
    """A result line whose cost exceeds the cap sets reason='budget_cap' and kills."""
    # Given: a result line reporting $5 cost; spec has $1 cap
    result_line = (
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "total_cost_usd": 5.0,
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "duration_ms": 1000,
            }
        )
        + "\n"
    )
    proc = _make_fake_proc([result_line])
    events_f = io.StringIO()
    log_f = io.StringIO()

    # When: _stream_to_events processes the line
    reason, _last_seq, total_cost = await _stream_to_events(
        proc,
        events_f,
        log_f,
        run_id="r",
        spec=_make_spec(budget_usd_cap=1.0),
        started_monotonic=time.monotonic(),
        starting_seq=0,
    )

    # Then: termination_reason is "budget_cap" and proc.kill() was invoked once
    assert reason == "budget_cap"
    assert total_cost == 5.0
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_stream_to_events_triggers_wallclock_cap_and_kills() -> None:
    """When wallclock elapsed exceeds the cap, reason='wallclock_cap' and kill issued."""
    # Given: a fake proc with no output; started_monotonic set far in the past
    proc = _make_fake_proc([])  # immediately EOF would also return, but we override below

    # Re-seed a reader that never EOFs but also has no data — so readline() would block.
    # The wallclock check fires BEFORE readline(), so we don't need data.
    reader = asyncio.StreamReader()
    reader.feed_data(b"")  # no newline → readline would block
    proc.stdout = reader

    events_f = io.StringIO()
    log_f = io.StringIO()

    # When: started_monotonic is 1 hour ago; cap is 10s
    past = time.monotonic() - 3600.0
    reason, _last_seq, _total_cost = await _stream_to_events(
        proc,
        events_f,
        log_f,
        run_id="r",
        spec=_make_spec(wallclock_sec_cap=10),
        started_monotonic=past,
        starting_seq=0,
    )

    # Then: reason is "wallclock_cap" and kill() was invoked
    assert reason == "wallclock_cap"
    proc.kill.assert_called_once()
