"""Event Store port definition.

This module defines the abstract interface for event storage and retrieval.
Infrastructure adapters (e.g., PostgreSQL) must implement this protocol.
"""

from typing import Protocol
from uuid import UUID

from core.domain.events import DomainEvent


class EventStorePort(Protocol):
    """Abstract interface for event persistence with Optimistic Concurrency Control.

    The event store is the single source of truth in our event-sourced system.
    All state changes are persisted as immutable events in an append-only log.

    Implementations must guarantee:
    - Atomic appends with version checking (OCC)
    - Events are retrieved in sequence_number order
    - Thread-safe/async-safe operations
    """

    async def append(self, event: DomainEvent, expected_version: int) -> None:
        """Append a domain event to the event stream.

        This method implements Optimistic Concurrency Control (OCC). The event
        will only be appended if the current version of the aggregate matches
        the expected_version. If versions don't match, a ConcurrencyError
        should be raised.

        Args:
            event: The domain event to append.
            expected_version: The expected current version of the aggregate.
                             If the actual version differs, append fails.

        Raises:
            ConcurrencyError: When expected_version doesn't match actual version.
            EventStoreError: On database or connection failures.
        """
        ...

    async def get_events(self, aggregate_id: UUID) -> list[DomainEvent]:
        """Retrieve all events for a specific aggregate, ordered by sequence_number.

        Events are returned in the order they were appended, allowing the
        aggregate state to be reconstructed via event replay.

        Args:
            aggregate_id: The UUID of the aggregate (AgentSession).

        Returns:
            List of domain events ordered by sequence_number (ascending).
            Returns empty list if aggregate doesn't exist.

        Raises:
            EventStoreError: On database or connection failures.
        """
        ...

    async def get_all_aggregate_ids(self) -> list[UUID]:
        """Retrieve all unique aggregate IDs that have events in the store.

        This method is used by the orchestration layer to discover all agents
        in the system and determine which ones are active.

        Returns:
            List of UUIDs for all aggregates that have at least one event.
            Returns empty list if no aggregates exist.

        Raises:
            EventStoreError: On database or connection failures.
        """
        ...
