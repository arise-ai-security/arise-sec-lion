"""Integration tests for PostgresEventStore.

These tests require a PostgreSQL database.

Setup with Docker Compose (recommended):
    # Start the one local PostgreSQL container and create its isolated test database
    docker compose -f deployment/docker-compose.yml --profile test up -d db
    # Run integration tests
    TEST_DB_URL="postgresql://arise@127.0.0.1:5432/arise_test" \\
        uv run pytest infrastructure/tests/test_event_store.py -v

If TEST_DB_URL environment variable is not set, tests will be skipped.
"""

import inspect
import os
from uuid import uuid4

import pytest

from core.domain.aggregates.agent_session import AgentRole
from core.domain.events.events import AgentCreated, TaskAssigned, WorkCompleted
from core.domain.exceptions import ConcurrencyError, EventStoreError
from core.ports.event_store_port import EventStoreWritePort
from infrastructure.adapters.postgres_event_store import PostgresEventStore


# Check if PostgreSQL test database is available
TEST_DB_URL = os.environ.get("TEST_DB_URL")
_skip_if_no_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL environment variable not set"
)


# D.3 signature assertions — pure introspection, do not require Postgres.
def test_append_signature_has_no_expected_version() -> None:
    """EventStoreWritePort.append no longer accepts expected_version."""

    # Given: the segregated write port
    sig = inspect.signature(EventStoreWritePort.append)

    # Then: the signature exposes only self and event, no version kwarg
    assert "expected_version" not in sig.parameters


def test_append_batch_signature_has_no_expected_version() -> None:
    """EventStoreWritePort.append_batch no longer accepts expected_version."""

    # Given: the segregated write port
    sig = inspect.signature(EventStoreWritePort.append_batch)

    # Then: the signature exposes only self and events, no version kwarg
    assert "expected_version" not in sig.parameters


@pytest.fixture
async def event_store():
    """Create and initialize event store for testing."""
    if not TEST_DB_URL:
        pytest.skip("TEST_DB_URL not set")

    store = PostgresEventStore(TEST_DB_URL)
    await store.connect()
    await store.initialize_schema()

    yield store

    # Cleanup: drop the test table
    if store.pool:
        async with store.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS events")

    await store.disconnect()


@pytest.mark.asyncio
async def test_event_store_initialization(event_store: PostgresEventStore):
    """Test that event store initializes schema correctly."""

    # Given: Event store is initialized (by fixture)
    # When: Query for table existence
    async with event_store.pool.acquire() as conn:
        table_exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'events'
            )
            """
        )

    # Then: Table should exist
    assert table_exists is True


@pytest.mark.asyncio
async def test_event_store_append_and_get_single_event(event_store: PostgresEventStore):
    """Test appending and retrieving a single event (happy path)."""

    # Given: Create a domain event
    aggregate_id = uuid4()
    event = AgentCreated(
        aggregate_id=aggregate_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={"model": "gpt-4"},
    )

    # When: Append the event
    await event_store.append(event)

    # And: Retrieve events for the aggregate
    events = await event_store.get_events(aggregate_id)

    # Then: Should return exactly one event
    assert len(events) == 1

    # And: Event should be correctly deserialized
    retrieved_event = events[0]
    assert isinstance(retrieved_event, AgentCreated)
    assert retrieved_event.aggregate_id == aggregate_id
    assert retrieved_event.sequence_number == 1
    assert retrieved_event.role == AgentRole.BOSS.value
    assert retrieved_event.config == {"model": "gpt-4"}


@pytest.mark.asyncio
async def test_count_events_returns_counts_for_requested_aggregates(
    event_store: PostgresEventStore,
):
    """Test counting events for multiple aggregates in one query."""

    # Given: Two aggregates with different event counts
    first_id = uuid4()
    second_id = uuid4()
    missing_id = uuid4()
    await event_store.append(
        AgentCreated(
            aggregate_id=first_id,
            sequence_number=1,
            role=AgentRole.BOSS.value,
            parent_id=None,
            config={},
        )
    )
    await event_store.append(
        TaskAssigned(
            aggregate_id=first_id,
            sequence_number=2,
            task_description="first task",
        )
    )
    await event_store.append(
        AgentCreated(
            aggregate_id=second_id,
            sequence_number=1,
            role=AgentRole.WORKER.value,
            parent_id=first_id,
            config={},
        )
    )

    # When: Counting events for both known aggregates and a missing aggregate
    counts = await event_store.count_events([first_id, second_id, missing_id])

    # Then: Counts are returned for all requested aggregates
    assert counts == {first_id: 2, second_id: 1, missing_id: 0}


@pytest.mark.asyncio
async def test_event_store_append_multiple_events(event_store: PostgresEventStore):
    """Test appending and retrieving multiple events in sequence."""

    # Given: Create an aggregate with multiple events
    aggregate_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=aggregate_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={"model": "gpt-4"},
    )

    event2 = TaskAssigned(
        aggregate_id=aggregate_id,
        sequence_number=2,
        task_description="Build a web scraper",
    )

    event3 = WorkCompleted(
        aggregate_id=aggregate_id, sequence_number=3, result="Successfully completed"
    )

    # When: Append events in sequence
    await event_store.append(event1)
    await event_store.append(event2)
    await event_store.append(event3)

    # And: Retrieve all events
    events = await event_store.get_events(aggregate_id)

    # Then: Should return all events in order
    assert len(events) == 3
    assert isinstance(events[0], AgentCreated)
    assert isinstance(events[1], TaskAssigned)
    assert isinstance(events[2], WorkCompleted)

    # And: Verify order
    assert events[0].sequence_number == 1
    assert events[1].sequence_number == 2
    assert events[2].sequence_number == 3


@pytest.mark.asyncio
async def test_event_store_get_events_empty_aggregate(event_store: PostgresEventStore):
    """Test retrieving events for non-existent aggregate returns empty list."""

    # Given: A random aggregate ID that doesn't exist
    non_existent_id = uuid4()

    # When: Retrieve events
    events = await event_store.get_events(non_existent_id)

    # Then: Should return empty list
    assert events == []


@pytest.mark.asyncio
async def test_event_store_concurrency_error_same_sequence(event_store: PostgresEventStore):
    """Test that OCC detects concurrent writes with same sequence_number."""

    # Given: Create two events with the same sequence_number
    aggregate_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=aggregate_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={"model": "gpt-4"},
    )

    event2_duplicate = AgentCreated(
        aggregate_id=aggregate_id,
        sequence_number=1,  # Same sequence!
        role=AgentRole.MANAGER.value,
        parent_id=uuid4(),
        config={"model": "claude-3"},
    )

    # When: Append first event
    await event_store.append(event1)

    # Then: Appending second event with same sequence should raise ConcurrencyError
    with pytest.raises(ConcurrencyError) as exc_info:
        await event_store.append(event2_duplicate)

    # And: Error should contain conflict details
    error = exc_info.value
    assert error.aggregate_id == str(aggregate_id)


@pytest.mark.asyncio
async def test_event_store_concurrency_error_duplicate_sequence(event_store: PostgresEventStore):
    """Test that OCC rejects a duplicate sequence_number even at a later position."""

    # Given: Append multiple events to establish a sequence
    aggregate_id = uuid4()

    event1 = AgentCreated(
        aggregate_id=aggregate_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
    )

    event2 = TaskAssigned(aggregate_id=aggregate_id, sequence_number=2, task_description="Task 1")

    event3_duplicate = TaskAssigned(
        aggregate_id=aggregate_id, sequence_number=2, task_description="Task 2"
    )

    # When: Append events
    await event_store.append(event1)
    await event_store.append(event2)

    # Then: Re-using sequence_number=2 should fail
    with pytest.raises(ConcurrencyError):
        await event_store.append(event3_duplicate)


@pytest.mark.asyncio
async def test_event_store_isolation_between_aggregates(event_store: PostgresEventStore):
    """Test that events for different aggregates are isolated."""

    # Given: Two different aggregates
    aggregate_a = uuid4()
    aggregate_b = uuid4()

    event_a = AgentCreated(
        aggregate_id=aggregate_a,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
    )

    event_b = AgentCreated(
        aggregate_id=aggregate_b,
        sequence_number=1,
        role=AgentRole.MANAGER.value,
        parent_id=uuid4(),
        config={},
    )

    # When: Append events to both aggregates
    await event_store.append(event_a)
    await event_store.append(event_b)

    # Then: Retrieving events for aggregate A returns only A's events
    events_a = await event_store.get_events(aggregate_a)
    assert len(events_a) == 1
    assert events_a[0].aggregate_id == aggregate_a
    assert events_a[0].role == AgentRole.BOSS.value

    # And: Retrieving events for aggregate B returns only B's events
    events_b = await event_store.get_events(aggregate_b)
    assert len(events_b) == 1
    assert events_b[0].aggregate_id == aggregate_b
    assert events_b[0].role == AgentRole.MANAGER.value


@pytest.mark.asyncio
async def test_event_store_preserves_metadata(event_store: PostgresEventStore):
    """Test that event metadata is preserved during serialization."""

    # Given: Create event with metadata
    aggregate_id = uuid4()
    event = AgentCreated(
        aggregate_id=aggregate_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
        metadata={"user_id": "user-123", "correlation_id": "corr-456"},
    )

    # When: Append and retrieve
    await event_store.append(event)
    events = await event_store.get_events(aggregate_id)

    # Then: Metadata should be preserved
    retrieved_event = events[0]
    assert retrieved_event.metadata == {"user_id": "user-123", "correlation_id": "corr-456"}


@pytest.mark.asyncio
async def test_event_store_error_without_connection():
    """Test that operations fail gracefully without connection."""

    # Given: Event store without connection
    store = PostgresEventStore("postgresql://invalid")

    # When/Then: Append without connect should raise EventStoreError
    event = AgentCreated(
        aggregate_id=uuid4(),
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
    )

    with pytest.raises(EventStoreError, match=r"(?i)connection pool not initialized"):
        await store.append(event)

    # And: Get events without connect should raise EventStoreError
    with pytest.raises(EventStoreError, match=r"(?i)connection pool not initialized"):
        await store.get_events(uuid4())

    # And: Count events without connect should raise EventStoreError
    with pytest.raises(EventStoreError, match=r"(?i)connection pool not initialized"):
        await store.count_events([uuid4()])

    # And: Initialize schema without connect should raise EventStoreError
    with pytest.raises(EventStoreError, match=r"(?i)connection pool not initialized"):
        await store.initialize_schema()
