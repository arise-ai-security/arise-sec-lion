"""Exceptions raised by the run-result analysis module."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from uuid import UUID


class AnalysisError(Exception):
    """Base class for all analysis-module errors."""


class UnknownFamilyError(AnalysisError):
    """Raised when ``compute_run_result`` is called with an unpopulated family."""

    def __init__(self, family: str) -> None:
        self.family = family
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"unknown family: {self.family!r} (taxonomy not populated)"


class NotABossRunError(AnalysisError):
    """Raised when the supplied ``run_id`` does not name a boss aggregate.

    A boss aggregate's first event is ``RunStarted`` on the same aggregate
    id as ``run_id``. Any other shape is rejected.
    """

    def __init__(
        self,
        run_id: UUID,
        first_event_type: str,
        first_aggregate_id: UUID,
    ) -> None:
        self.run_id = run_id
        self.first_event_type = first_event_type
        self.first_aggregate_id = first_aggregate_id
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"{self.run_id} is not a boss aggregate "
            f"(first event: {self.first_event_type} on {self.first_aggregate_id})"
        )


class UnknownRunError(AnalysisError):
    """Raised when no events can be found for the supplied ``run_id``."""

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"no events found for run_id={self.run_id}"
