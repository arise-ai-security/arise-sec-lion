"""Outcome metrics: run status, work/verification counts, failure-mode classifier.

Design doc: ``agent-docs/2026-05-17-run-result-quantitative-design.md`` §5, §8.

Walks an event sequence ONCE and folds the relevant signals into a frozen
``OutcomeMetrics``. The failure-mode classifier reads ``WorkFailed.reason``
text and an optional ``exit_status`` field from event metadata; see §8 for
the priority rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from experiments.shared.scripts.analysis.models import OutcomeMetrics


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from experiments.shared.scripts.db.models import EventRow


_RE_HARD: Final[re.Pattern[str]] = re.compile(r"Timed out after \d+s$")
_RE_INACTIVITY: Final[re.Pattern[str]] = re.compile(
    r"Timed out after \d+s without Claude output",
)
_RE_PROVIDER: Final[re.Pattern[str]] = re.compile(
    r"\b(api|provider|429|rate.?limit|quota|overload|"
    r"anthropic|openai|litellm)\b",
    re.IGNORECASE,
)

_RUN_STATUS_IN_PROGRESS: Final[str] = "in_progress"


_SUCCESS_EXIT_STATUSES: Final[frozenset[str | None]] = frozenset({None, "success", "completed"})


def _classify_from_reason(reason: str) -> str:
    """Map a non-empty ``WorkFailed.reason`` to a failure-mode label."""
    if _RE_HARD.search(reason):
        return "hard_timeout"
    if _RE_INACTIVITY.search(reason):
        return "inactivity_timeout"
    if _RE_PROVIDER.search(reason):
        return "provider_failure"
    return "work_error"


def classify_failure(reason: str | None, exit_status: str | None) -> str | None:
    """Classify a boss failure into one of the five documented labels.

    Returns ``hard_timeout`` / ``inactivity_timeout`` / ``provider_failure`` /
    ``work_error`` / ``None``. Priority follows design doc §8:

      1. ``exit_status == "timeout"`` => hard vs inactivity based on reason text.
      2. ``reason`` text patterns.
      3. ``exit_status`` outside the success set => generic work_error.
    """
    if exit_status == "timeout":
        if reason and _RE_INACTIVITY.search(reason):
            return "inactivity_timeout"
        return "hard_timeout"
    if reason:
        return _classify_from_reason(reason)
    if exit_status not in _SUCCESS_EXIT_STATUSES:
        return "work_error"
    return None


@dataclass
class _OutcomeAccumulator:
    """Mutable per-walk accumulator. Folded into a frozen ``OutcomeMetrics``."""

    has_run_completed: bool = False
    has_work_completed: bool = False
    has_work_failed: bool = False
    work_completed_count: int = 0
    work_failed_count: int = 0
    verification_passed_count: int = 0
    verification_failed_count: int = 0
    verification_failed_stages: dict[str, int] = field(default_factory=dict)
    retries: int = 0
    redecompositions: int = 0
    decisions_infeasible: int = 0
    run_status: str = _RUN_STATUS_IN_PROGRESS
    boss_failure_reason: str | None = None
    boss_work_failed_exit_status: str | None = None
    boss_run_completed_exit_status: str | None = None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _handle_work_completed(acc: _OutcomeAccumulator, _ev: EventRow, on_boss: bool) -> None:
    acc.work_completed_count += 1
    if on_boss:
        acc.has_work_completed = True


def _handle_work_failed(acc: _OutcomeAccumulator, ev: EventRow, on_boss: bool) -> None:
    acc.work_failed_count += 1
    if not on_boss:
        return
    acc.has_work_failed = True
    acc.boss_failure_reason = _str_or_none((ev.payload or {}).get("reason"))
    acc.boss_work_failed_exit_status = _str_or_none((ev.metadata or {}).get("exit_status"))


def _handle_run_completed(acc: _OutcomeAccumulator, ev: EventRow, on_boss: bool) -> None:
    if not on_boss:
        return
    acc.has_run_completed = True
    status = _str_or_none((ev.payload or {}).get("status"))
    if status is not None:
        acc.run_status = status
    acc.boss_run_completed_exit_status = _str_or_none((ev.metadata or {}).get("exit_status"))


def _handle_verification_failed(
    acc: _OutcomeAccumulator,
    ev: EventRow,
    _on_boss: bool,
) -> None:
    acc.verification_failed_count += 1
    stage = _str_or_none((ev.payload or {}).get("failed_stage"))
    if stage is not None:
        acc.verification_failed_stages[stage] = acc.verification_failed_stages.get(stage, 0) + 1


def _handle_verification_passed(
    acc: _OutcomeAccumulator,
    _ev: EventRow,
    _on_boss: bool,
) -> None:
    acc.verification_passed_count += 1


def _handle_retry(acc: _OutcomeAccumulator, _ev: EventRow, _on_boss: bool) -> None:
    acc.retries += 1


def _handle_redecomp(acc: _OutcomeAccumulator, _ev: EventRow, _on_boss: bool) -> None:
    acc.redecompositions += 1


def _handle_infeasible(acc: _OutcomeAccumulator, _ev: EventRow, _on_boss: bool) -> None:
    acc.decisions_infeasible += 1


_EVENT_HANDLERS: Final[
    dict[str, Callable[[_OutcomeAccumulator, EventRow, bool], None]]
] = {
    "WorkCompleted": _handle_work_completed,
    "WorkFailed": _handle_work_failed,
    "RunCompleted": _handle_run_completed,
    "VerificationPassed": _handle_verification_passed,
    "VerificationFailed": _handle_verification_failed,
    "RetryScheduled": _handle_retry,
    "RedecompositionTriggered": _handle_redecomp,
    "DecisionInfeasible": _handle_infeasible,
}


def compute_outcomes(
    events: Sequence[EventRow],
    *,
    run_id: UUID,
) -> OutcomeMetrics:
    """Fold an event sequence into an ``OutcomeMetrics``.

    Walks ``events`` once. Boss-only fields (``has_*``, ``run_status``,
    ``failure_mode``, ``failure_reason``, ``worker_exit_status``) filter on
    ``aggregate_id == run_id``; count fields aggregate across all aggregates.
    If the boss emits multiple ``WorkFailed`` events the LAST one wins
    (latest terminal state). ``run_status`` likewise reflects the last
    ``RunCompleted`` on the boss.
    """
    acc = _OutcomeAccumulator()
    for ev in events:
        handler = _EVENT_HANDLERS.get(ev.event_type)
        if handler is not None:
            handler(acc, ev, ev.aggregate_id == run_id)

    denom = acc.verification_passed_count + acc.verification_failed_count
    verification_pass_rate: float | None = (
        acc.verification_passed_count / denom if denom > 0 else None
    )
    worker_exit_status = (
        acc.boss_work_failed_exit_status
        if acc.boss_work_failed_exit_status is not None
        else acc.boss_run_completed_exit_status
    )
    failure_mode = classify_failure(acc.boss_failure_reason, worker_exit_status)

    return OutcomeMetrics(
        run_status=acc.run_status,
        has_run_completed=acc.has_run_completed,
        has_work_completed=acc.has_work_completed,
        has_work_failed=acc.has_work_failed,
        work_completed_count=acc.work_completed_count,
        work_failed_count=acc.work_failed_count,
        verification_passed_count=acc.verification_passed_count,
        verification_failed_count=acc.verification_failed_count,
        verification_pass_rate=verification_pass_rate,
        verification_failed_stages=acc.verification_failed_stages,
        retries=acc.retries,
        redecompositions=acc.redecompositions,
        decisions_infeasible=acc.decisions_infeasible,
        failure_mode=failure_mode,
        failure_reason=acc.boss_failure_reason,
        worker_exit_status=worker_exit_status,
    )
