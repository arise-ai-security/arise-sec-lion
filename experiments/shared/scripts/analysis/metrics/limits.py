"""LimitEnforced metric computer (design doc §5)."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from experiments.shared.scripts.analysis.models import LimitMetrics


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.scripts.db.models import EventRow


def compute_limits(events: Sequence[EventRow]) -> LimitMetrics:
    """Count LimitEnforced events grouped by limit_type."""
    by_type: defaultdict[str, int] = defaultdict(int)
    total = 0
    for e in events:
        if e.event_type != "LimitEnforced":
            continue
        total += 1
        limit_type = str(e.payload.get("limit_type") or "unknown")
        by_type[limit_type] += 1
    return LimitMetrics(total_enforcements=total, by_limit_type=dict(by_type))
