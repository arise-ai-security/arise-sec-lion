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
        {
            "event_type": "PromptSent",
            "prompt": "do work",
            "prompt_type": "worker_execution",
            "target": "claude_code",
        },
        {
            "event_type": "ThoughtCaptured",
            "content": "ls -la",
            "output_type": "tool_use",
            "tool_name": "Bash",
        },
        {
            "event_type": "ThoughtCaptured",
            "content": "Tool result: ok",
            "output_type": "tool_result",
        },
        {
            "event_type": "ThoughtCaptured",
            "content": "visible reasoning",
            "output_type": "thinking",
        },
        {
            "event_type": "WorkerCostRecorded",
            "tool_name": "claude_code",
            "prompt_tokens": 5,
            "completion_tokens": 6,
            "reasoning_tokens": 1,
            "cost_usd": 0.1234567,
            "duration_seconds": 2.5,
        },
        {
            "event_type": "RunCompleted",
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
    # Audit N-3: total comes from the breakdown sum (5 + 6 + 0 + 0 + 1).
    assert metrics["tokens_total"] == 12
    assert metrics["worker_cost_usd"] == pytest.approx(0.123457)
    assert metrics["total_cost_usd"] == pytest.approx(0.123457)
    assert metrics["run_duration_seconds"] == pytest.approx(10.125)
    assert metrics["run_completed_count"] == 1
    assert metrics["tool_calls_by_type"] == {"Bash": 1}


def test_worker_cost_recorded_includes_cache_tokens() -> None:
    # Given: a WorkerCostRecorded-shaped event with all five token buckets.
    events = [
        {
            "event_type": "WorkerCostRecorded",
            "tool_name": "claude_code",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cache_read_tokens": 1000,
            "cache_write_tokens": 200,
            "reasoning_tokens": 3,
            "cost_usd": 0.01,
            "duration_seconds": 1.0,
        }
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: total is the breakdown sum (audit N-3)
    assert metrics["tokens_total"] == 10 + 5 + 1000 + 200 + 3
    # And: cache buckets surface as their own columns for cross-adapter audit
    assert metrics["tokens_cache_read"] == 1000
    assert metrics["tokens_cache_write"] == 200


def test_run_duration_seconds_missing_run_completed_returns_sentinel() -> None:
    # Given: events without a RunCompleted
    events = [
        {
            "event_type": "ThoughtCaptured",
            "content": "ls",
            "output_type": "tool_use",
            "tool_name": "Bash",
        },
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: sentinel -1.0 (loud) instead of silently masquerading as 0.0
    assert metrics["run_duration_seconds"] == -1.0
    assert metrics["run_completed_count"] == 0


def test_float_coerces_nan_to_zero() -> None:
    # Given: an event whose cost_usd is NaN
    events = [
        {
            "event_type": "WorkerCostRecorded",
            "tool_name": "claude_code",
            "prompt_tokens": 1,
            "cost_usd": float("nan"),
            "duration_seconds": 1.0,
        }
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: NaN is dropped, the rollup is not poisoned
    import math as _math
    assert _math.isfinite(metrics["worker_cost_usd"])
    assert _math.isfinite(metrics["total_cost_usd"])
    assert metrics["worker_cost_usd"] == 0.0


def test_float_coerces_inf_to_zero() -> None:
    # Given: an event whose cost_usd is +Infinity
    events = [
        {
            "event_type": "WorkerCostRecorded",
            "tool_name": "claude_code",
            "prompt_tokens": 1,
            "cost_usd": float("inf"),
            "duration_seconds": 1.0,
        }
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: Infinity is dropped
    import math as _math
    assert _math.isfinite(metrics["worker_cost_usd"])
    assert metrics["worker_cost_usd"] == 0.0


def test_event_type_uses_explicit_discriminator_for_dict_input() -> None:
    # Given: a dict event with an explicit ``event_type`` discriminator
    # (the post-N-5 on-disk format).
    events = [
        {
            "event_type": "AgentCreated",
            "role": "BOSS",
        },
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: event_count is incremented (the event was classified, not unknown)
    assert metrics["event_count"] == 1


def test_tool_calls_by_type_uses_structured_tool_name() -> None:
    # Given: a tool_use event with a structured `tool_name` field.
    events = [
        {
            "event_type": "ThoughtCaptured",
            "content": "ls -la",
            "output_type": "tool_use",
            "tool_name": "Bash",
        },
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: the canonical name is recorded.
    assert metrics["tool_calls_by_type"] == {"Bash": 1}


def test_tool_calls_by_type_unlabeled_tool_use_records_unknown() -> None:
    # Given: a tool_use event with no structured tool_name field. Adapters
    # are expected to emit `tool_name=` post-audit; missing values bucket
    # as "unknown" so analysts can spot the gap.
    events = [
        {
            "event_type": "ThoughtCaptured",
            "content": "Tool: Read\nargs: {}",
            "output_type": "tool_use",
        },
    ]

    # When
    metrics = metrics_from_events(events)

    # Then: unlabeled tool calls bucket as "unknown"
    assert metrics["tool_calls_by_type"] == {"unknown": 1}


def test_metrics_from_events_jsonl_rejects_malformed_lines(tmp_path) -> None:
    # Given: a JSONL file with one corrupted line between valid ones.
    # Pre-N-1, the reader silently skipped the bad line and produced a
    # downcounted row; post-N-1 the writer is atomic so any corrupted line
    # represents out-of-band damage and the reader must refuse.
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({
                    "event_type": "ThoughtCaptured",
                    "content": "ls",
                    "output_type": "tool_use",
                    "tool_name": "Bash",
                }),
                "not-json",
                json.dumps({
                    "event_type": "ThoughtCaptured",
                    "content": "ok",
                    "output_type": "tool_result",
                }),
            ]
        ),
        encoding="utf-8",
    )

    # When/Then: the reader raises with the offending line number
    with pytest.raises(ValueError, match=r":2: malformed JSON line"):
        metrics_from_events_jsonl(path)
