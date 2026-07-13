"""Unit tests for PostgresEventStore pool sizing (G.1) and SQL tiebreaker (G.2).

The pool-sizing test patches ``asyncpg.create_pool`` so it runs without a
live database. The SQL tiebreaker test requires Postgres and is gated by
``TEST_DB_URL``.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.domain.aggregates.agent_session import AgentRole
from core.domain.events.events import (
    AgentCreated,
    RetryScheduled,
    TaskAssigned,
    WorkCompleted,
)
from infrastructure.adapters.postgres_event_store import PostgresEventStore


# ----------------------------------------------------------------------------
# G.1 — pool sizing
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_size_kwargs_forwarded_to_asyncpg() -> None:
    """PostgresEventStore must forward pool_min/pool_max to asyncpg."""

    # Given: a PostgresEventStore with non-default pool sizes
    store = PostgresEventStore(
        "postgresql://fake",
        pool_min=3,
        pool_max=7,
    )

    # When: connect() is called and asyncpg.create_pool is patched out
    fake_pool = AsyncMock()
    with patch(
        "infrastructure.adapters.postgres_event_store.asyncpg.create_pool",
        new=AsyncMock(return_value=fake_pool),
    ) as mock_create_pool:
        await store.connect()

    # Then: the pool was created with min_size=3, max_size=7
    mock_create_pool.assert_awaited_once()
    _, kwargs = mock_create_pool.call_args
    assert kwargs["min_size"] == 3
    assert kwargs["max_size"] == 7


@pytest.mark.asyncio
async def test_pool_size_defaults_match_historical_asyncpg_values() -> None:
    """Default pool sizing remains (10, 10) so unchanged deployments are
    bit-for-bit identical to pre-G.1 behaviour.
    """

    # Given: a PostgresEventStore with no pool overrides
    store = PostgresEventStore("postgresql://fake")

    # When: connect is called
    fake_pool = AsyncMock()
    with patch(
        "infrastructure.adapters.postgres_event_store.asyncpg.create_pool",
        new=AsyncMock(return_value=fake_pool),
    ) as mock_create_pool:
        await store.connect()

    # Then: asyncpg sees the historical defaults
    _, kwargs = mock_create_pool.call_args
    assert kwargs["min_size"] == 10
    assert kwargs["max_size"] == 10


# ----------------------------------------------------------------------------
# G.2 — Wall-clock projection tiebreaker
# ----------------------------------------------------------------------------


TEST_DB_URL = os.environ.get("TEST_DB_URL")
_skip_if_no_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DB_URL environment variable not set"
)


@pytest.fixture
async def event_store():
    """Real PostgresEventStore fixture for the G.2 SQL tiebreaker test."""
    if not TEST_DB_URL:
        pytest.skip("TEST_DB_URL not set")

    store = PostgresEventStore(TEST_DB_URL)
    await store.connect()
    await store.initialize_schema()

    yield store

    if store.pool:
        async with store.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS events")
    await store.disconnect()


@_skip_if_no_db
@pytest.mark.asyncio
async def test_initialize_schema_serializes_concurrent_callers() -> None:
    """Concurrent processes can initialize the same fresh database safely."""

    # Given: two independent event stores connected to a database without the events table
    first = PostgresEventStore(TEST_DB_URL, pool_min=1, pool_max=1)
    second = PostgresEventStore(TEST_DB_URL, pool_min=1, pool_max=1)
    await asyncio.gather(first.connect(), second.connect())
    assert first.pool is not None

    try:
        async with first.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS events")

        # When: both stores initialize the schema concurrently
        await asyncio.gather(first.initialize_schema(), second.initialize_schema())

        # Then: initialization succeeds and creates exactly one usable table
        async with first.pool.acquire() as conn:
            table_exists = await conn.fetchval("SELECT to_regclass('public.events') IS NOT NULL")
        assert table_exists is True
    finally:
        if first.pool:
            async with first.pool.acquire() as conn:
                await conn.execute("DROP TABLE IF EXISTS events")
        await asyncio.gather(first.disconnect(), second.disconnect())


@_skip_if_no_db
@pytest.mark.asyncio
async def test_latest_status_uses_sequence_tiebreaker_on_wall_clock_ties(
    event_store: PostgresEventStore,
) -> None:
    """When two status events share occurred_at, sequence_number breaks the tie.

    RetryScheduled (seq=10) followed by WorkCompleted (seq=11) sharing the
    same occurred_at must project to status='completed'.
    """

    # Given: a BOSS agent with TaskAssigned, then two status events at
    # the same occurred_at but increasing sequence_number.
    agent_id = uuid4()
    shared_time = datetime.now(UTC)

    boss_created = AgentCreated(
        aggregate_id=agent_id,
        sequence_number=1,
        role=AgentRole.BOSS.value,
        parent_id=None,
        config={},
        occurred_at=shared_time - timedelta(seconds=5),
    )
    task_assigned = TaskAssigned(
        aggregate_id=agent_id,
        sequence_number=2,
        task_description="Top task",
        occurred_at=shared_time - timedelta(seconds=4),
    )
    retry_event = RetryScheduled(
        aggregate_id=agent_id,
        sequence_number=10,
        attempt=1,
        reason="transient",
        occurred_at=shared_time,
    )
    work_completed = WorkCompleted(
        aggregate_id=agent_id,
        sequence_number=11,
        result="done",
        occurred_at=shared_time,
    )

    # When: events are persisted in arbitrary order
    await event_store.append(boss_created)
    await event_store.append(task_assigned)
    await event_store.append(retry_event)
    await event_store.append(work_completed)

    # Then: the summary projection picks WorkCompleted (higher seq wins).
    summaries = await event_store.get_boss_agent_summaries(limit=10, offset=0)
    summary = next(s for s in summaries if s["agent_id"] == agent_id)
    assert summary["status"] == "completed"
