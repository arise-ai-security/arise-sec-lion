"""Domain tests for the PatchPlanApproved provenance emitter."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import PatchPlanApproved


def _agent() -> AgentSession:
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0, "max_tokens": 100},
        "tool": "claude_code",
    }
    agent = AgentSession.create(uuid4(), AgentRole.MANAGER, config)
    agent.assign_task("[Fixer] fix")
    return agent


def test_record_patch_plan_approved_emits_frozen_plan_identity() -> None:
    # Given: a Fixer manager with a validated, frozen patch plan
    agent = _agent()

    # When: the approved plan is recorded
    agent.record_patch_plan_approved(
        plan_sha256="a" * 64, evidence_references=["event:root-cause"]
    )

    # Then: a PatchPlanApproved event carries the frozen plan identity
    event = agent.events[-1]
    assert isinstance(event, PatchPlanApproved)
    assert event.plan_sha256 == "a" * 64
    assert event.evidence_references == ["event:root-cause"]


def test_patch_plan_approved_survives_replay() -> None:
    # Given: an agent that recorded an approved patch plan
    agent = _agent()
    agent.record_patch_plan_approved(plan_sha256="b" * 64, evidence_references=[])

    # When: replayed from history
    restored = AgentSession.load_from_history(agent.events)

    # Then: replay applies the event without error (version advances identically)
    assert restored.version == agent.version
