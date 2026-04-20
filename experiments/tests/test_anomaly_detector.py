"""Tests for the experiment-runner anomaly detector."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from experiments.anomaly_detector import (
    STUCK_TIME_THRESHOLD_SECONDS,
    Anomaly,
    AnomalyHalt,
    detect_anomalies,
    run_ended_cleanly,
    write_anomaly_report,
)


def _ev(
    event_type: str,
    *,
    agent_id: str | None = None,
    role: str | None = None,
    occurred_at: datetime | None = None,
    payload: dict | None = None,
) -> dict:
    ts = occurred_at or datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    ev: dict = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "occurred_at": ts.isoformat(),
        "agent_id": agent_id,
        "role": role,
        "sequence_number": 1,
        "payload": payload or {},
    }
    return ev


def _write_events(run_dir: Path, events: list[dict]) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return events_path


def test_detect_anomalies_returns_empty_for_clean_run(tmp_path: Path) -> None:
    """A run that ends with run_completed and has no failures is anomaly-free."""

    # Given: A clean events.jsonl ending with run_completed
    run_dir = tmp_path / "clean"
    aid = str(uuid4())
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=aid, role="BOSS"),
        _ev("tool_use", agent_id=aid, role="BOSS"),
        _ev("run_completed"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: no anomalies
    assert anomalies == []


def test_detects_malformed_json_after_retries(tmp_path: Path) -> None:
    """A work_failed event whose reason contains the marker is flagged."""

    # Given: events.jsonl with a BOSS that hit the malformed_json_after_retries marker
    run_dir = tmp_path / "malformed"
    aid = str(uuid4())
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=aid, role="BOSS"),
        _ev(
            "work_failed",
            agent_id=aid,
            role="BOSS",
            payload={
                "reason": (
                    "malformed_json_after_retries: Decomposition failed after "
                    "3 attempts: LLM response is not valid JSON"
                )
            },
        ),
        _ev("run_completed"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: one anomaly flagged with the right kind
    assert len(anomalies) == 1
    assert anomalies[0].kind == "malformed_json_after_retries"
    assert anomalies[0].agent_id == aid
    assert "malformed_json_after_retries" in anomalies[0].evidence


def test_detects_malformed_json_under_generic_payload_nesting(tmp_path: Path) -> None:
    """WorkFailed serialises through GenericPayload, so ``reason`` lives under
    ``payload.data.reason`` not ``payload.reason``. The detector MUST handle
    both shapes. Regression test for the bug caught in the Pillar B v2 dry-run
    where a malformed_json_after_retries B2 run was not halted because the
    detector only read the un-nested shape.
    """

    # Given: a WorkFailed event where the reason is nested under ``data``
    run_dir = tmp_path / "malformed-nested"
    aid = str(uuid4())
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=aid, role="BOSS"),
        _ev(
            "work_failed",
            agent_id=aid,
            role="BOSS",
            payload={
                "data": {
                    "reason": (
                        "malformed_json_after_retries: Decomposition failed after "
                        "3 attempts: LLM response is not valid JSON"
                    )
                }
            },
        ),
        _ev("run_completed"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: the nested reason is extracted and the marker fires
    assert any(a.kind == "malformed_json_after_retries" for a in anomalies)


def test_detects_worker_timeout_with_zero_tool_calls(tmp_path: Path) -> None:
    """A WORKER that timed out without ever calling a tool is flagged."""

    # Given: a WORKER with no tool_use events and a work_failed reason containing 'timeout'
    run_dir = tmp_path / "timeout"
    aid = str(uuid4())
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=aid, role="WORKER"),
        _ev(
            "work_failed",
            agent_id=aid,
            role="WORKER",
            payload={"reason": "Worker subprocess exceeded timeout of 600s"},
        ),
        _ev("run_completed"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: the worker_timeout_zero_tools kind fires
    assert any(a.kind == "worker_timeout_zero_tools" for a in anomalies)


def test_does_not_flag_worker_timeout_when_tool_calls_were_made(tmp_path: Path) -> None:
    """A WORKER that DID make tool calls but timed out is a business outcome, not an anomaly."""

    # Given: WORKER with tool_use events before timeout
    run_dir = tmp_path / "legit-timeout"
    aid = str(uuid4())
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=aid, role="WORKER"),
        _ev("tool_use", agent_id=aid, role="WORKER"),
        _ev("tool_use", agent_id=aid, role="WORKER"),
        _ev(
            "work_failed",
            agent_id=aid,
            role="WORKER",
            payload={"reason": "timeout after 600s"},
        ),
        _ev("run_completed"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: no worker_timeout_zero_tools flagged (it did make tool calls)
    assert not any(a.kind == "worker_timeout_zero_tools" for a in anomalies)


def test_detects_worker_stuck_on_large_event_gap(tmp_path: Path) -> None:
    """A WORKER with consecutive events separated by >STUCK_TIME_THRESHOLD_SECONDS fires worker_stuck."""

    # Given: WORKER events spaced further than the threshold apart
    run_dir = tmp_path / "stuck"
    aid = str(uuid4())
    t0 = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=STUCK_TIME_THRESHOLD_SECONDS + 30)
    events = [
        _ev("run_started", occurred_at=t0),
        _ev("agent_created", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("tool_use", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("tool_use", agent_id=aid, role="WORKER", occurred_at=t1),
        _ev("run_completed", occurred_at=t1),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: worker_stuck fires with the agent id
    stuck = [a for a in anomalies if a.kind == "worker_stuck"]
    assert len(stuck) == 1
    assert stuck[0].agent_id == aid
    assert "gap" in stuck[0].evidence


def test_detects_truncated_run_when_run_completed_missing(tmp_path: Path) -> None:
    """A truncated events.jsonl (no run_completed) is flagged as uncaught_orchestration_exception."""

    # Given: events.jsonl that ends before run_completed
    run_dir = tmp_path / "truncated"
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=str(uuid4()), role="BOSS"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: uncaught_orchestration_exception fires
    assert any(a.kind == "uncaught_orchestration_exception" for a in anomalies)


def test_detects_missing_events_file(tmp_path: Path) -> None:
    """If events.jsonl does not exist, uncaught_orchestration_exception fires."""

    # Given: a run dir without events.jsonl
    run_dir = tmp_path / "missing"
    run_dir.mkdir()

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: exactly one uncaught_orchestration_exception
    assert len(anomalies) == 1
    assert anomalies[0].kind == "uncaught_orchestration_exception"
    assert "not written" in anomalies[0].evidence


def test_write_anomaly_report_round_trips(tmp_path: Path) -> None:
    """write_anomaly_report serialises anomalies to anomaly.json as a list of dicts."""

    # Given: two anomalies
    run_dir = tmp_path / "report"
    run_dir.mkdir()
    anomalies = [
        Anomaly(kind="malformed_json_after_retries", agent_id="abc", evidence="x"),
        Anomaly(kind="worker_stuck", agent_id="def", evidence="y"),
    ]

    # When: write the report
    path = write_anomaly_report(run_dir, anomalies)

    # Then: file is a JSON array of dicts carrying the three fields
    assert path == run_dir / "anomaly.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert data[0] == {"kind": "malformed_json_after_retries", "agent_id": "abc", "evidence": "x"}
    assert data[1]["agent_id"] == "def"


def test_worker_stuck_ignored_when_large_gap_ends_in_work_completed(tmp_path: Path) -> None:
    """A long-running WORKER that finishes cleanly via work_completed is not flagged stuck.

    The worker emits agent_created + tool_use at t0, then work_completed at
    t0+200s. The 200s gap between tool_use and work_completed does NOT count
    because work_completed is explicitly excluded from the gap scan.
    """

    # Given: a tool_use at t0 and a terminal work_completed 200s later
    run_dir = tmp_path / "legit-long"
    aid = str(uuid4())
    t0 = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=STUCK_TIME_THRESHOLD_SECONDS + 80)
    events = [
        _ev("run_started", occurred_at=t0),
        _ev("agent_created", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("tool_use", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("work_completed", agent_id=aid, role="WORKER", occurred_at=t1),
        _ev("run_completed", occurred_at=t1),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: no worker_stuck (work_completed filtered, so only agent_created + tool_use
    # remain as gap-eligible and both are at t0)
    assert not any(a.kind == "worker_stuck" for a in anomalies)


def test_worker_stuck_detected_on_silent_worker_that_then_fails(tmp_path: Path) -> None:
    """A WORKER that goes silent for >threshold and then fails IS flagged stuck.

    This is the case Codex raised as a regression: if the detector excluded
    work_failed from the gap scan, a stuck worker that eventually timed out
    would escape both the ``worker_timeout_zero_tools`` path (because it
    made earlier tool calls) and the ``worker_stuck`` path. work_failed must
    therefore participate in gap detection.
    """

    # Given: tool_use at t0, then a 200s silence, then a work_failed
    run_dir = tmp_path / "silent-then-fail"
    aid = str(uuid4())
    t0 = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=STUCK_TIME_THRESHOLD_SECONDS + 80)
    events = [
        _ev("run_started", occurred_at=t0),
        _ev("agent_created", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("tool_use", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev(
            "work_failed",
            agent_id=aid,
            role="WORKER",
            occurred_at=t1,
            payload={"reason": "subprocess hung indefinitely"},
        ),
        _ev("run_completed", occurred_at=t1),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: worker_stuck fires on the 200s silence-then-failure
    stuck = [a for a in anomalies if a.kind == "worker_stuck"]
    assert len(stuck) == 1
    assert stuck[0].agent_id == aid


def test_worker_stuck_skips_single_event_agents(tmp_path: Path) -> None:
    """A WORKER with fewer than two gap-eligible events cannot have a measurable gap."""

    # Given: a WORKER with agent_created only (no tool_use, no work_* terminal)
    run_dir = tmp_path / "single-event"
    aid = str(uuid4())
    events = [
        _ev("run_started"),
        _ev("agent_created", agent_id=aid, role="WORKER"),
        _ev("run_completed"),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: no worker_stuck — the len(entries) < 2 guard in the detector
    # short-circuits before attempting to compute any gap
    assert not any(a.kind == "worker_stuck" for a in anomalies)


def test_worker_stuck_ignores_out_of_order_timestamps(tmp_path: Path) -> None:
    """A negative delta between consecutive events is treated as log corruption, not a stuck signal."""

    # Given: WORKER events whose timestamps run backwards (clock skew / reordering)
    run_dir = tmp_path / "out-of-order"
    aid = str(uuid4())
    t0 = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
    t_earlier = t0 - timedelta(seconds=500)
    events = [
        _ev("run_started", occurred_at=t0),
        _ev("agent_created", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("tool_use", agent_id=aid, role="WORKER", occurred_at=t0),
        _ev("tool_use", agent_id=aid, role="WORKER", occurred_at=t_earlier),
        _ev("run_completed", occurred_at=t0),
    ]
    _write_events(run_dir, events)

    # When: detect_anomalies runs
    anomalies = detect_anomalies(run_dir)

    # Then: the negative delta is skipped (not treated as a valid stuck signal)
    assert not any(a.kind == "worker_stuck" for a in anomalies)


def test_run_ended_cleanly_true_when_last_event_is_run_completed(tmp_path: Path) -> None:
    """run_ended_cleanly returns True when events.jsonl terminates with run_completed."""

    # Given: an events.jsonl ending with run_completed
    run_dir = tmp_path / "clean-end"
    _write_events(run_dir, [_ev("run_started"), _ev("run_completed")])

    # When/Then: the helper reports clean termination
    assert run_ended_cleanly(run_dir) is True


def test_run_ended_cleanly_false_on_truncated_events(tmp_path: Path) -> None:
    """run_ended_cleanly returns False when events.jsonl does not end with run_completed."""

    # Given: a truncated events.jsonl
    run_dir = tmp_path / "truncated-end"
    _write_events(run_dir, [_ev("run_started")])

    # When/Then: the helper reports NOT clean
    assert run_ended_cleanly(run_dir) is False


def test_run_ended_cleanly_false_when_missing(tmp_path: Path) -> None:
    """run_ended_cleanly returns False when events.jsonl is absent entirely."""

    # Given: a run dir with no events.jsonl
    run_dir = tmp_path / "missing-end"
    run_dir.mkdir()

    # When/Then: the helper reports NOT clean
    assert run_ended_cleanly(run_dir) is False


def test_anomaly_halt_message_includes_every_kind(tmp_path: Path) -> None:
    """The AnomalyHalt exception message includes every anomaly kind for operator visibility."""

    # Given: two anomalies
    run_dir = tmp_path / "halt"
    anomalies = [
        Anomaly(kind="malformed_json_after_retries", agent_id=None, evidence="a"),
        Anomaly(kind="worker_stuck", agent_id=None, evidence="b"),
    ]

    # When: construct the halt exception
    halt = AnomalyHalt(run_dir, anomalies)

    # Then: stringified form includes both kinds
    msg = str(halt)
    assert "malformed_json_after_retries" in msg
    assert "worker_stuck" in msg
    assert str(run_dir) in msg
