"""Domain events for the multi-agent system.

This module defines the base event class and all domain events used in the
event sourcing architecture. All events are immutable (frozen) Pydantic models.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    """Get current UTC datetime (Python 3.12+ compatible)."""
    return datetime.now(UTC)


class DomainEvent(BaseModel):
    """Base class for all domain events.

    Domain events represent facts that have occurred in the system. They are
    immutable and used to reconstruct aggregate state via event sourcing.

    Attributes:
        event_id: Unique identifier for this event instance.
        aggregate_id: ID of the aggregate (AgentSession) this event belongs to.
        sequence_number: Order of this event in the aggregate's event stream.
        occurred_at: Timestamp when the event occurred.
        metadata: Additional context about the event (e.g., user_id, correlation_id).
    """

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    aggregate_id: UUID
    sequence_number: int
    occurred_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
