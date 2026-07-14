"""RuntimeSurfaceSealed must be registered for event-store replay (Criterion 9)."""

from uuid import uuid4

import orjson

from core.domain.events.events import RuntimeSurfaceSealed, SealedArtifact
from infrastructure.adapters.postgres_event_store import EVENT_TYPE_REGISTRY


def test_runtime_surface_sealed_registered_and_round_trips() -> None:
    """The event reconstructs from a JSON payload with nested artifacts intact."""

    # Given: the event is registered so get_events can deserialize it on replay.
    assert EVENT_TYPE_REGISTRY.get("RuntimeSurfaceSealed") is RuntimeSurfaceSealed

    original = RuntimeSurfaceSealed(
        aggregate_id=uuid4(),
        sequence_number=2,
        surface="sample",
        sealed_artifacts=[
            SealedArtifact(
                container_path="/artifacts/bootstrap.sh",
                kind="bootstrap_script",
                content_sha256="d" * 64,
            ),
            SealedArtifact(container_path="/usr/local/bin/runner", kind="runtime_wrapper"),
        ],
    )

    # When: serialized to JSON (as the store persists it) and rebuilt via the registry.
    payload = orjson.loads(orjson.dumps(original.model_dump(mode="json")))
    restored = EVENT_TYPE_REGISTRY["RuntimeSurfaceSealed"](**payload)

    # Then: the round-trip preserves the event and its nested SealedArtifacts.
    assert restored == original
    assert [a.kind for a in restored.sealed_artifacts] == [
        "bootstrap_script",
        "runtime_wrapper",
    ]
    assert restored.sealed_artifacts[0].content_sha256 == "d" * 64
    assert restored.sealed_artifacts[1].non_golden is True
