"""Tests for infeasible decision and probing (Phase 5)."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    DecisionInfeasible,
    ProbeCompleted,
    ProbeStarted,
    RedecompositionTriggered,
)


def _config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


class TestDecisionInfeasible:
    """Agent marks task as infeasible within constraints."""

    def test_mark_infeasible_transitions_to_failed(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.MANAGER, config=_config(), parent_id=uuid4()
        )
        agent.assign_task("Complex task")
        agent.mark_infeasible(reason="Too few subtasks allowed", minimum_subtasks=5)

        assert agent.status == AgentStatus.FAILED
        assert "Infeasible" in agent.error_message

    def test_infeasible_event_has_minimum_requirements(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.MANAGER, config=_config(), parent_id=uuid4()
        )
        agent.assign_task("Task")
        agent.mark_infeasible(
            reason="Need more depth", minimum_subtasks=3, minimum_depth=2
        )

        infeasible_events = [e for e in agent.events if isinstance(e, DecisionInfeasible)]
        assert len(infeasible_events) == 1
        assert infeasible_events[0].reason == "Need more depth"
        assert infeasible_events[0].minimum_subtasks == 3
        assert infeasible_events[0].minimum_depth == 2

    def test_infeasible_replays_correctly(self) -> None:
        """Event sourcing: infeasible state survives replay."""
        agent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.MANAGER, config=_config(), parent_id=uuid4()
        )
        agent.assign_task("Task")
        agent.mark_infeasible(reason="Constraints too tight")

        # Replay from events
        all_events = list(agent.events)
        agent.mark_changes_as_committed()
        replayed = AgentSession.load_from_history(all_events)

        assert replayed.status == AgentStatus.FAILED
        assert "Infeasible" in replayed.error_message


class TestRedecomposition:
    """Parent re-decomposes when child signals infeasible."""

    def test_redecomposition_transitions_to_analyzing(self) -> None:
        parent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.BOSS, config=_config()
        )
        parent.assign_task("Main task")

        # Simulate WAITING state (normally set by spawning children)
        from core.domain.events.events import StatusChanged

        waiting_event = StatusChanged(
            aggregate_id=parent.agent_id,
            sequence_number=parent._next_sequence(),
            old_status="analyzing",
            new_status="waiting",
            reason="Spawned children",
        )
        parent._apply(waiting_event)
        parent._changes.append(waiting_event)
        assert parent.status == AgentStatus.WAITING

        child_id = uuid4()
        parent.trigger_redecomposition(
            trigger_child_id=child_id, reason="Child infeasible"
        )

        assert parent.status == AgentStatus.ANALYZING
        assert parent.redecomposition_count == 1

    def test_redecomposition_clears_children(self) -> None:
        parent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.BOSS, config=_config()
        )
        parent.assign_task("Task")

        # Add fake children
        from core.domain.events.events import ChildSpawned
        from core.domain.values.subtask import Subtask

        child_id = uuid4()
        spawn_event = ChildSpawned(
            aggregate_id=parent.agent_id,
            sequence_number=parent._next_sequence(),
            child_id=child_id,
            child_role="PENDING",
            subtask=Subtask(description="Sub", config=_config()),
            child_config=_config(),
        )
        parent._apply(spawn_event)
        parent._changes.append(spawn_event)
        assert len(parent.child_ids) == 1

        # Redecompose
        parent.trigger_redecomposition(
            trigger_child_id=child_id, reason="Infeasible"
        )
        assert len(parent.child_ids) == 0

    def test_redecomposition_event_persisted(self) -> None:
        parent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.BOSS, config=_config()
        )
        parent.assign_task("Task")

        parent.trigger_redecomposition(
            trigger_child_id=uuid4(), reason="Constraint failure"
        )

        redecomp_events = [
            e for e in parent.events if isinstance(e, RedecompositionTriggered)
        ]
        assert len(redecomp_events) == 1
        assert redecomp_events[0].reason == "Constraint failure"

    def test_redecomposition_count_replays_correctly(self) -> None:
        parent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.BOSS, config=_config()
        )
        parent.assign_task("Task")
        parent.trigger_redecomposition(
            trigger_child_id=uuid4(), reason="Constraint failure"
        )

        all_events = list(parent.events)
        parent.mark_changes_as_committed()
        replayed = AgentSession.load_from_history(all_events)

        assert replayed.redecomposition_count == 1
        assert replayed.status == AgentStatus.ANALYZING


class TestProbeEvents:
    """Probe events for observability."""

    def test_probe_events_dont_change_status(self) -> None:
        agent = AgentSession.create(
            agent_id=uuid4(), role=AgentRole.PENDING, config=_config(), parent_id=uuid4()
        )
        agent.assign_task("Task")
        assert agent.status == AgentStatus.ANALYZING

        # Emit probe events
        start_event = ProbeStarted(
            aggregate_id=agent.agent_id,
            sequence_number=agent._next_sequence(),
            probe_type="file_check",
        )
        agent._apply(start_event)
        agent._changes.append(start_event)

        complete_event = ProbeCompleted(
            aggregate_id=agent.agent_id,
            sequence_number=agent._next_sequence(),
            probe_type="file_check",
            result_summary="Found 3 relevant files",
        )
        agent._apply(complete_event)
        agent._changes.append(complete_event)

        # Status unchanged
        assert agent.status == AgentStatus.ANALYZING

        # Events recorded
        probe_events = [
            e for e in agent.events
            if isinstance(e, (ProbeStarted, ProbeCompleted))
        ]
        assert len(probe_events) == 2
