"""Run timing metrics (design doc §5).

Computes wall-clock and per-event-duration aggregates from a sequence of
``EventRow`` records. Single-pass: walks the events ONCE, dispatching on
``event_type`` and aggregating into per-field accumulators.

Boss-scoped fields (``run_started_at``, ``run_completed_at``,
``run_duration_seconds``) require ``event.aggregate_id == run_id``. The
totals (``agent_execution_total_seconds``, ``operation_total_seconds``,
``operation_seconds_by_type``) are summed across ALL aggregates so that
child-agent runtime is accounted for.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.models import TimingMetrics


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from experiments.shared.scripts.db.models import EventRow


def compute_timing(events: Sequence[EventRow], *, run_id: UUID) -> TimingMetrics:
    """Compute :class:`TimingMetrics` from a sequence of events.

    Args:
        events: Events to aggregate (any order).
        run_id: Boss aggregate id; used to filter the boss-scoped fields.

    Returns:
        Fully populated :class:`TimingMetrics`. Boss-scoped fields are
        ``None`` when the corresponding event is missing; totals default
        to ``0.0`` and the per-type map defaults to an empty ``dict``.
    """
    run_started_at = None
    run_completed_at = None
    run_duration_seconds: float | None = None
    agent_execution_total_seconds = 0.0
    operation_total_seconds = 0.0
    operation_seconds_by_type: defaultdict[str, float] = defaultdict(float)

    for event in events:
        event_type = event.event_type
        payload = event.payload
        if event_type == "RunStarted" and event.aggregate_id == run_id:
            run_started_at = event.occurred_at
        elif event_type == "RunCompleted" and event.aggregate_id == run_id:
            run_completed_at = event.occurred_at
            raw_duration = payload.get("duration_seconds")
            if raw_duration is not None:
                run_duration_seconds = float(raw_duration)
        elif event_type == "AgentExecutionFinished":
            agent_execution_total_seconds += float(payload.get("duration_seconds") or 0.0)
        elif event_type == "OperationFinished":
            duration = float(payload.get("duration_seconds") or 0.0)
            operation_total_seconds += duration
            operation_type = payload.get("operation_type")
            if isinstance(operation_type, str):
                operation_seconds_by_type[operation_type] += duration

    wall_clock_seconds: float | None = None
    if run_started_at is not None and run_completed_at is not None:
        wall_clock_seconds = (run_completed_at - run_started_at).total_seconds()

    return TimingMetrics(
        run_started_at=run_started_at,
        run_completed_at=run_completed_at,
        wall_clock_seconds=wall_clock_seconds,
        run_duration_seconds=run_duration_seconds,
        agent_execution_total_seconds=agent_execution_total_seconds,
        operation_total_seconds=operation_total_seconds,
        operation_seconds_by_type=dict(operation_seconds_by_type),
    )
