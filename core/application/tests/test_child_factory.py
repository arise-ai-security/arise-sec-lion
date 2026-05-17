"""Atomic reservation tests for ChildAgentFactory (D.2)."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from core.application.services.lifecycle.child_factory import ChildAgentFactory
from core.application.services.lifecycle.hierarchy_limits_registry import (
    HierarchyLimitsRegistry,
)


def _make_factory(max_total: int) -> ChildAgentFactory:
    """Build a factory with mocked repo + empty limits registry."""
    return ChildAgentFactory(
        repository=AsyncMock(),
        limits_registry=HierarchyLimitsRegistry(),
        max_total_agents=max_total,
    )


@pytest.mark.asyncio
async def test_reserve_release_under_concurrent_managers() -> None:
    """Two concurrent reserve(3) calls against cap=5 — exactly one wins."""

    # Given: a factory with cap=5 (one slot was already committed)
    factory = _make_factory(max_total=5)
    factory.reset(initial_count=0)

    # When: two concurrent reservations for 3 slots each race
    results = await asyncio.gather(
        factory.try_reserve(3),
        factory.try_reserve(3),
    )

    # Then: exactly one succeeds; the other fails without bumping reserved
    assert sorted(results) == [False, True]
    assert factory.reserved == 3


@pytest.mark.asyncio
async def test_reserve_release_unwinds_on_persist_failure() -> None:
    """A reserved-then-released slot is reusable on the next reserve."""

    # Given: a factory with cap=3 and an initial reservation of 3
    factory = _make_factory(max_total=3)
    factory.reset(initial_count=0)
    assert await factory.try_reserve(3) is True
    assert factory.reserved == 3

    # When: the persist fails and the orchestrator releases the slots
    await factory.release_reservation(3)

    # Then: the next reservation sees the full cap again
    assert factory.reserved == 0
    assert await factory.try_reserve(3) is True


@pytest.mark.asyncio
async def test_commit_reservation_moves_count_atomically() -> None:
    """commit_reservation decrements reserved and increments total_created."""

    # Given: a factory with cap=4 and 2 reserved
    factory = _make_factory(max_total=4)
    factory.reset(initial_count=0)
    assert await factory.try_reserve(2) is True
    assert factory.reserved == 2
    assert factory.total_created == 0

    # When: those slots are committed
    await factory.commit_reservation(2)

    # Then: the slots show as created, the reservation is cleared
    assert factory.reserved == 0
    assert factory.total_created == 2


@pytest.mark.asyncio
async def test_unlimited_factory_skips_reservation_accounting() -> None:
    """An unlimited factory (max=-1) treats try_reserve as a no-op success."""

    # Given: an unlimited factory
    factory = _make_factory(max_total=-1)

    # When: a reservation is attempted
    assert await factory.try_reserve(10) is True

    # Then: nothing is tracked against the cap
    assert factory.reserved == 0
    assert factory.total_created == 0

    # And: commit still bumps total_created so observers track real spawns
    await factory.commit_reservation(4)
    assert factory.total_created == 4
