"""Detect infrastructure-level anomalies in a completed run.

Purpose
-------
A run failure can be either:

* a **business outcome** -- the agent tried, didn't produce a correct patch,
  and the evaluator marked ``mechanical_pass=False``. Dataset-valid.
* an **infrastructure anomaly** -- something in the runner / orchestrator
  / adapter crashed or stalled in a way that makes the resulting row
  uninterpretable. The operator needs to fix code, not accept the result.

This module inspects a completed run's artifacts and flags anomalies so the
experiment queue can halt, the operator can patch the bug, and the run can be
re-executed cleanly.

Detected anomaly kinds
----------------------
``malformed_json_after_retries``
    A BOSS / MANAGER exhausted ``_JSON_PARSE_RETRIES`` attempts without
    emitting parseable JSON. Detected via the structured
    ``MALFORMED_JSON_REASON_PREFIX`` stamped on the ``work_failed`` reason
    by ``core.application.agent_orchestrator``.

``worker_timeout_zero_tools``
    A WORKER timed out without making any tool call at all (``tool_use``
    event count == 0 for that agent). This typically signals the worker
    subprocess never launched or hung before its first API call.

``worker_stuck``
    The same WORKER has a gap greater than
    :data:`STUCK_TIME_THRESHOLD_SECONDS` between consecutive events while it
    is the active agent. Usually indicates a hung subprocess or a runaway
    LLM call.

``uncaught_orchestration_exception``
    Events file is missing or does not end with a ``run_completed`` event,
    suggesting the runner process was killed mid-write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

#: Gap (seconds) between consecutive events on a single WORKER before the run
#: is flagged as ``worker_stuck``. Set at 15 min to absorb legitimately long
#: tool operations (full-project Bash builds, container orchestration,
#: large-file reads). The Pillar B v2 dry-run revealed 940 s legitimate
#: single-tool gaps on mruby B1 that the original 120 s threshold flagged
#: as stuck. A real hung subprocess will still be caught by the per-worker
#: timeout (``worker.timeout = 600 s`` in cell configs) + the wallclock cap.
STUCK_TIME_THRESHOLD_SECONDS = 900.0

AnomalyKind = Literal[
    "malformed_json_after_retries",
    "worker_timeout_zero_tools",
    "worker_stuck",
    "uncaught_orchestration_exception",
]


@dataclass(frozen=True, slots=True)
class Anomaly:
    """A single structural problem detected in a run."""

    kind: AnomalyKind
    agent_id: str | None
    evidence: str

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "agent_id": self.agent_id, "evidence": self.evidence}


def detect_anomalies(run_dir: Path) -> list[Anomaly]:
    """Inspect a completed run directory and return every detected anomaly.

    Args:
        run_dir: The run directory (containing ``events.jsonl``).

    Returns:
        A list of :class:`Anomaly`. Empty means the run is dataset-valid
        regardless of ``mechanical_pass`` outcome.
    """
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return [
            Anomaly(
                kind="uncaught_orchestration_exception",
                agent_id=None,
                evidence=f"{events_path.name} was not written",
            )
        ]

    events = _load_events(events_path)
    if not events:
        return [
            Anomaly(
                kind="uncaught_orchestration_exception",
                agent_id=None,
                evidence=f"{events_path.name} is empty",
            )
        ]

    anomalies: list[Anomaly] = []
    anomalies.extend(_detect_malformed_json(events))
    anomalies.extend(_detect_worker_timeouts(events))
    anomalies.extend(_detect_worker_stuck(events))
    anomalies.extend(_detect_truncated_run(events))
    return anomalies


def _load_events(events_path: Path) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping un-parseable event line in %s", events_path)
    return out


def _extract_reason(payload: object) -> str:
    """Pull the human-readable reason out of a ``work_failed`` payload.

    WorkFailed events serialise through ``GenericPayload``, which nests
    the domain event's fields under ``payload.data``. Handle both shapes:

    * ``payload = {"reason": "..."}`` (future dedicated payload model).
    * ``payload = {"data": {"reason": "..."}}`` (current v1/v2 projection).
    """
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("reason")
    if isinstance(direct, str) and direct:
        return direct
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("reason")
        if isinstance(nested, str):
            return nested
    return ""


def _detect_malformed_json(events: list[dict[str, object]]) -> list[Anomaly]:
    # Marker string set in core.application.agent_orchestrator when all retries
    # exhaust. Kept here as a literal to avoid a core→experiments coupling.
    marker = "malformed_json_after_retries"
    out: list[Anomaly] = []
    for ev in events:
        if ev.get("event_type") != "work_failed":
            continue
        reason = _extract_reason(ev.get("payload"))
        if marker in reason:
            out.append(
                Anomaly(
                    kind="malformed_json_after_retries",
                    agent_id=_as_str(ev.get("agent_id")),
                    evidence=reason[:500],
                )
            )
    return out


def _detect_worker_timeouts(events: list[dict[str, object]]) -> list[Anomaly]:
    tool_counts: dict[str, int] = {}
    for ev in events:
        if ev.get("event_type") != "tool_use":
            continue
        aid = _as_str(ev.get("agent_id"))
        if aid:
            tool_counts[aid] = tool_counts.get(aid, 0) + 1

    out: list[Anomaly] = []
    for ev in events:
        if ev.get("event_type") != "work_failed" or ev.get("role") != "WORKER":
            continue
        reason = _extract_reason(ev.get("payload"))
        aid = _as_str(ev.get("agent_id")) or ""
        if "timeout" in reason.lower() and tool_counts.get(aid, 0) == 0:
            out.append(
                Anomaly(
                    kind="worker_timeout_zero_tools",
                    agent_id=aid,
                    evidence=f"WORKER timed out with 0 tool calls: {reason[:200]}",
                )
            )
    return out


#: ``work_completed`` is terminal-success; the preceding gap represents
#: legitimate long work that eventually succeeded, so we exclude it from
#: the gap scan to avoid false positives.
#:
#: ``work_failed`` is intentionally NOT excluded: a worker that went silent
#: and then failed is precisely the "stuck" signal we care about, even if
#: it made earlier tool calls (so ``worker_timeout_zero_tools`` wouldn't
#: catch it).
_TERMINAL_WORKER_EVENT_TYPES = frozenset({"work_completed"})


def _detect_worker_stuck(events: list[dict[str, object]]) -> list[Anomaly]:
    # Preserve events.jsonl insertion order per agent. We do NOT sort by
    # timestamp -- out-of-order arrivals on the same agent are a separate bug
    # and should not be smoothed over into a false "stuck" positive.
    by_agent: dict[str, list[tuple[datetime, str]]] = {}
    for ev in events:
        if ev.get("role") != "WORKER":
            continue
        event_type = _as_str(ev.get("event_type")) or ""
        if event_type in _TERMINAL_WORKER_EVENT_TYPES:
            continue
        aid = _as_str(ev.get("agent_id"))
        ts_raw = ev.get("occurred_at")
        if not aid or not isinstance(ts_raw, str):
            continue
        ts = _parse_iso(ts_raw)
        if ts is None:
            continue
        by_agent.setdefault(aid, []).append((ts, event_type))

    out: list[Anomaly] = []
    for aid, entries in by_agent.items():
        if len(entries) < 2:
            continue
        for (prev_ts, _prev_type), (cur_ts, _cur_type) in zip(
            entries, entries[1:], strict=False
        ):
            delta = (cur_ts - prev_ts).total_seconds()
            # Negative delta = out-of-order log; don't guess, skip.
            if delta < 0:
                continue
            if delta > STUCK_TIME_THRESHOLD_SECONDS:
                out.append(
                    Anomaly(
                        kind="worker_stuck",
                        agent_id=aid,
                        evidence=(
                            f"{delta:.0f}s gap between consecutive WORKER events "
                            f"(> {STUCK_TIME_THRESHOLD_SECONDS:.0f}s)"
                        ),
                    )
                )
                break  # one flag per agent is enough
    return out


def _detect_truncated_run(events: list[dict[str, object]]) -> list[Anomaly]:
    last_type = events[-1].get("event_type")
    if last_type == "run_completed":
        return []
    return [
        Anomaly(
            kind="uncaught_orchestration_exception",
            agent_id=None,
            evidence=f"events.jsonl did not end with run_completed (last event: {last_type!r})",
        )
    ]


def run_ended_cleanly(run_dir: Path) -> bool:
    """Return True iff ``<run_dir>/events.jsonl`` ends with a ``run_completed`` event.

    Shared with :func:`experiments.run_experiment.is_resumable` so the "ran
    to completion" contract cannot drift between the resume gate and the
    anomaly detector.
    """
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return False
    events = _load_events(events_path)
    if not events:
        return False
    return events[-1].get("event_type") == "run_completed"


def _as_str(value: object | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_iso(ts: str) -> datetime | None:
    # Python's fromisoformat accepts "...+00:00" but not "...Z"; normalise.
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_anomaly_report(run_dir: Path, anomalies: list[Anomaly]) -> Path:
    """Write ``<run_dir>/anomaly.json`` with the list of anomalies.

    Returns the path written. Caller is responsible for raising the halt
    signal after this file is on disk.
    """
    path = run_dir / "anomaly.json"
    payload = [a.to_dict() for a in anomalies]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class AnomalyHalt(Exception):
    """Raised by the experiment runner when an anomaly is detected.

    The queue driver catches this, prints a summary, and exits non-zero so
    the operator knows code needs to be fixed before resuming.
    """

    def __init__(self, run_dir: Path, anomalies: list[Anomaly]) -> None:
        self.run_dir = run_dir
        self.anomalies = anomalies
        kinds = ", ".join(a.kind for a in anomalies)
        super().__init__(f"Anomaly in {run_dir}: [{kinds}]")
