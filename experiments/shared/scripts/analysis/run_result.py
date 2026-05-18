"""Composer for ``RunResult`` — builds per-run quantitative metrics.

The composer is the single entry point external callers use to turn a boss
aggregate id into a fully populated :class:`RunResult`. It owns the
run-id contract (design doc §11) — empty event lists raise
:class:`UnknownRunError` and non-boss first events raise
:class:`NotABossRunError`. Family validation is delegated to
``compute_tools``, which raises :class:`UnknownFamilyError` for any
family not declared in ``TOOL_TAXONOMY``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.errors import (
    NotABossRunError,
    UnknownRunError,
)
from experiments.shared.scripts.analysis.metrics.cost import compute_cost
from experiments.shared.scripts.analysis.metrics.hierarchy import compute_hierarchy
from experiments.shared.scripts.analysis.metrics.limits import compute_limits
from experiments.shared.scripts.analysis.metrics.outcomes import compute_outcomes
from experiments.shared.scripts.analysis.metrics.timing import compute_timing
from experiments.shared.scripts.analysis.metrics.tools import compute_tools
from experiments.shared.scripts.analysis.models import QuantitativeMetrics, RunResult
from experiments.shared.scripts.db import fetch_run_events


if TYPE_CHECKING:
    from uuid import UUID

    import asyncpg


async def compute_run_result(
    conn: asyncpg.Connection,
    run_id: UUID,
    *,
    family: str = "A",
) -> RunResult:
    """Build the per-run quantitative metrics for a boss aggregate.

    Args:
        conn: Open asyncpg connection to the events store.
        run_id: Boss aggregate id (the run root).
        family: Cell family used to select the tool taxonomy. Defaults to
            ``"A"`` for the A1/A2 cohort.

    Returns:
        Fully populated :class:`RunResult` for ``run_id``.

    Raises:
        UnknownRunError: No events found for ``run_id``.
        NotABossRunError: First event is not ``RunStarted`` on ``run_id``
            (the supplied id names a non-boss aggregate).
        UnknownFamilyError: ``family`` is not declared in
            ``TOOL_TAXONOMY``. Bubbles up from ``compute_tools``.
    """
    events = await fetch_run_events(conn, run_id)
    if not events:
        raise UnknownRunError(run_id=run_id)
    root = events[0]
    if root.event_type != "RunStarted" or root.aggregate_id != run_id:
        raise NotABossRunError(
            run_id=run_id,
            first_event_type=root.event_type,
            first_aggregate_id=root.aggregate_id,
        )

    qm = QuantitativeMetrics(
        run_id=run_id,
        family=family,
        event_count=len(events),
        aggregate_count=len({e.aggregate_id for e in events}),
        cost=compute_cost(events),
        tools=compute_tools(events, family=family),
        outcomes=compute_outcomes(events, run_id=run_id),
        hierarchy=compute_hierarchy(events, run_id=run_id),
        timing=compute_timing(events, run_id=run_id),
        limits=compute_limits(events),
    )
    return RunResult(run_id=run_id, quantitative=qm)
