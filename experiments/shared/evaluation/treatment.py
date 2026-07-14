"""Read configured treatment metadata from run events."""

from core.domain.events.events import RunStarted
from experiments.shared.evaluation.common import events_of_type
from experiments.shared.evaluation.models import RunData


def treatment_version(run_data: RunData) -> str | None:
    """Return the latest recorded treatment identifier."""
    started = list(events_of_type(run_data.events, RunStarted))
    recorded = next(
        (event.treatment_version for event in reversed(started) if event.treatment_version),
        None,
    )
    return recorded
