"""Data model shared between the SQL and transform layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True)
class EventRow:
    """One row from the `events` table after JSONB decoding.

    Mirrors the columns 1:1 so callers can reason about the DB shape
    directly. `payload` and `metadata` are decoded dicts.
    """

    event_id: UUID
    aggregate_id: UUID
    sequence_number: int
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
