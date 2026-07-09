"""Tests for the RuntimeSurfaceSealed anti-leak event on AgentSession."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import RuntimeSurfaceSealed, SealedArtifact


def _boss_config() -> dict[str, Any]:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


def test_emit_runtime_surface_sealed_records_event_on_boss() -> None:
    """Sealing the runtime surface appends a RuntimeSurfaceSealed event."""

    # Given: a freshly created BOSS aggregate.
    agent_id = uuid4()
    boss = AgentSession.create(
        agent_id=agent_id, role=AgentRole.BOSS, config=_boss_config(), parent_id=None
    )
    version_before = boss.version
    artifacts = [
        SealedArtifact(
            container_path="/testcase/repro.sh", kind="repro_skeleton", content_sha256="a" * 64
        ),
        SealedArtifact(container_path="/usr/local/bin/secb", kind="secb_wrapper"),
    ]

    # When: Arise seals the agent-visible runtime surface.
    boss.emit_runtime_surface_sealed(surface="secbench", sealed_artifacts=artifacts)

    # Then: a RuntimeSurfaceSealed event is recorded carrying the sealed artifacts.
    event = boss.events[-1]
    assert isinstance(event, RuntimeSurfaceSealed)
    assert event.aggregate_id == agent_id
    assert event.surface == "secbench"
    assert [a.kind for a in event.sealed_artifacts] == ["repro_skeleton", "secb_wrapper"]

    # And: the secb_wrapper artifact defaults to the non-golden anti-leak marker.
    assert event.sealed_artifacts[1].non_golden is True

    # And: the aggregate version advanced for OCC.
    assert boss.version == version_before + 1


def test_sealed_artifact_is_frozen() -> None:
    """SealedArtifact is an immutable value object."""

    # Given: a sealed artifact.
    artifact = SealedArtifact(container_path="/testcase/patch.sh", kind="patch_script")

    # When/Then: reassignment is rejected by the frozen model.
    with pytest.raises(ValidationError):
        artifact.kind = "other"  # type: ignore[misc]
