"""Tests for scripted run metrics."""

from __future__ import annotations

import json

import pytest

from experiments.shared.scripts.run_metrics import (
    metrics_from_events,
    metrics_from_events_jsonl,
)


def test_metrics_from_raw_events_counts_tools_and_thinking() -> None:
    events = [
        {"prompt": "do work", "prompt_type": "worker_execution", "target": "claude_code"},
        {"content": "Tool: Bash\nInput: {}", "output_type": "tool_use"},
        {"content": "Tool result: ok", "output_type": "tool_result"},
        {"content": "visible reasoning", "output_type": "thinking"},
        {
            "tool_name": "claude_code",
            "tokens": 12,
            "prompt_tokens": 5,
            "completion_tokens": 6,
            "reasoning_tokens": 1,
            "cost_usd": 0.1234567,
            "duration_seconds": 2.5,
        },
        {
            "status": "completed",
            "duration_seconds": 10.1254,
            "total_agents": 1,
            "completed_agents": 1,
            "failed_agents": 0,
        },
    ]

    metrics = metrics_from_events(events)

    assert metrics["event_count"] == 6
    assert metrics["prompt_sent_count"] == 1
    assert metrics["tool_call_count"] == 1
    assert metrics["tool_result_count"] == 1
    assert metrics["thinking_event_count"] == 1
    assert metrics["thinking_chars"] == len("visible reasoning")
    assert metrics["tokens_total"] == 12
    assert metrics["worker_cost_usd"] == pytest.approx(0.123457)
    assert metrics["total_cost_usd"] == pytest.approx(0.123457)
    assert metrics["run_duration_seconds"] == pytest.approx(10.125)
    assert metrics["tool_calls_by_type"] == {"Bash": 1}


def test_metrics_from_events_jsonl_rejects_malformed_lines(tmp_path) -> None:
    # Given: a JSONL file with one corrupted line between valid ones.
    # Pre-N-1, the reader silently skipped the bad line and produced a
    # downcounted row; post-N-1 the writer is atomic so any corrupted line
    # represents out-of-band damage and the reader must refuse.
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"content": "Tool: Read", "output_type": "tool_use"}),
                "not-json",
                json.dumps({"content": "Tool result: contents", "output_type": "tool_result"}),
            ]
        ),
        encoding="utf-8",
    )

    # When/Then: the reader raises with the offending line number
    with pytest.raises(ValueError, match=r":2: malformed JSON line"):
        metrics_from_events_jsonl(path)
