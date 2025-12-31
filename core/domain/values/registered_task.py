from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RegisteredTask:
    """Value object representing a registered task for deduplication."""

    task_key: str
    task_description: str
    registered_by: UUID
    parent_id: UUID | None
