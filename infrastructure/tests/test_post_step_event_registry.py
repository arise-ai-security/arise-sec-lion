"""Post-step events must be registered for PostgreSQL replay."""

from uuid import uuid4

import orjson

from core.domain.events.events import PostStepCompleted, PostStepRequested
from infrastructure.adapters.postgres_event_store import EVENT_TYPE_REGISTRY


def test_post_step_events_registered_and_round_trip() -> None:
    """PostgreSQL payloads reconstruct the correlated post-step events."""

    # Given: a requested and completed post-step for one terminal outcome.
    aggregate_id = uuid4()
    terminal_event_id = uuid4()
    original_events = [
        PostStepRequested(
            aggregate_id=aggregate_id,
            sequence_number=8,
            terminal_event_id=terminal_event_id,
        ),
        PostStepCompleted(
            aggregate_id=aggregate_id,
            sequence_number=9,
            terminal_event_id=terminal_event_id,
        ),
    ]

    # When: events are encoded as JSONB payloads and rebuilt through the registry.
    restored_events = [
        EVENT_TYPE_REGISTRY[type(event).__name__](
            **orjson.loads(orjson.dumps(event.model_dump(mode="json")))
        )
        for event in original_events
    ]

    # Then: both classes are registered and retain their terminal correlation.
    assert EVENT_TYPE_REGISTRY["PostStepRequested"] is PostStepRequested
    assert EVENT_TYPE_REGISTRY["PostStepCompleted"] is PostStepCompleted
    assert restored_events == original_events
    assert restored_events[0].terminal_event_id == restored_events[1].terminal_event_id
