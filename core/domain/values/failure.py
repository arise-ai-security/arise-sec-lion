"""Failure-context value objects for retry and redecomposition prompts."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ThoughtExcerpt:
    """Bounded excerpt of one worker tool output, retained for failure digests."""

    tool_name: str | None
    output_type: str
    content: str


@dataclass(frozen=True, slots=True)
class ChildFailureRecord:
    """What a parent retains about one failed child for informed re-planning."""

    child_id: UUID
    child_task: str
    reason: str
    digest: str | None = None
