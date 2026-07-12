"""Treatment-version classification without rewriting historical events."""

from core.domain.events.events import RunStarted
from experiments.shared.evaluation.common import events_of_type
from experiments.shared.evaluation.models import RunData


def treatment_version(run_data: RunData, *, cell: str) -> str | None:
    """Return recorded treatment, classifying legacy unversioned B4 in place."""
    started = list(events_of_type(run_data.events, RunStarted))
    recorded = next(
        (event.treatment_version for event in reversed(started) if event.treatment_version),
        None,
    )
    if recorded is not None:
        return recorded
    return "b4-full-v0" if cell == "B4" else None
