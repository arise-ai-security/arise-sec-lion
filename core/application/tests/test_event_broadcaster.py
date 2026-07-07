"""Tests for the in-memory EventBroadcaster subscription registry."""

import logging
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from core.application.services.query.event_broadcaster import EventBroadcaster


if TYPE_CHECKING:
    from core.domain.events.events import DomainEvent


def _event() -> "DomainEvent":
    # publish() only forwards the object into subscriber queues; any sentinel
    # works at runtime.
    return cast("DomainEvent", object())


@pytest.mark.asyncio
async def test_publish_delivers_and_unsubscribe_clears_registry() -> None:
    """Round-trip: subscribe -> publish -> receive; context exit empties the registry."""

    # Given: one subscriber for a root
    broadcaster = EventBroadcaster()
    root_id = uuid4()
    event = _event()

    async with broadcaster.subscribe(root_id) as queue:
        # When: an event is published for that root
        await broadcaster.publish(event, root_id)

        # Then: the subscriber's queue receives it
        assert queue.get_nowait() is event

    # And: exiting the context removes the registration entirely
    assert root_id not in broadcaster._subscribers


@pytest.mark.asyncio
async def test_unsubscribe_survives_externally_cleared_registry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a vanished registration must not crash unsubscribe nor
    resurrect the root_id key (the registry is a defaultdict; the old code
    indexed it during cleanup, recreating the key as a leaked empty list)."""

    # Given: a subscription whose registration disappears mid-lifetime
    broadcaster = EventBroadcaster()
    root_id = uuid4()

    with caplog.at_level(logging.WARNING):
        async with broadcaster.subscribe(root_id):
            broadcaster._subscribers.clear()

    # Then: the anomaly is logged rather than swallowed
    assert "already missing" in caplog.text

    # And: the failed unsubscribe did not resurrect the key
    assert root_id not in broadcaster._subscribers
