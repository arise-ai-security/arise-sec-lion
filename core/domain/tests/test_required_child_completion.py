"""Tests for required and optional child completion invariants."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.values.subtask import Subtask


def _parent(subtasks: list[Subtask]):
    config = {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0, "max_tokens": 100},
        "tool": "claude_code",
    }
    parent = AgentSession.create(uuid4(), AgentRole.MANAGER, config)
    parent.assign_task("phase")
    children = parent.apply_subtasks_and_spawn_children(
        subtasks,
        child_role=AgentRole.WORKER.value,
        briefing=parent.build_briefing_for_child(),
    )
    return parent, [child_id for child_id, _ in children]


def test_required_child_failure_prevents_parent_completion() -> None:
    # Given: One failed required child and one successful required child
    parent, children = _parent(
        [
            Subtask(description="required producer", config={}, criticality="required"),
            Subtask(description="required consumer", config={}, criticality="required"),
        ]
    )

    # When: Both children report terminal outcomes
    parent.handle_child_failure(children[0], "producer failed")
    parent.handle_child_update(children[1], "consumer result")

    # Then: WorkCompleted cannot override the required failure
    assert parent.status == AgentStatus.FAILED


def test_optional_child_failure_may_be_tolerated() -> None:
    # Given: One failed optional child and one successful required child
    parent, children = _parent(
        [
            Subtask(description="optional specialist", config={}, criticality="optional"),
            Subtask(description="required outcome", config={}, criticality="required"),
        ]
    )

    # When: Both children report terminal outcomes
    parent.handle_child_failure(children[0], "optional evidence unavailable")
    parent.handle_child_update(children[1], "phase passed")

    # Then: The required outcome permits parent completion
    assert parent.status == AgentStatus.COMPLETED


def _chain(policy: str = "block") -> tuple:
    """A hard producer(0) -> consumer(1, depends_on=[0]) chain, both required."""
    return _parent(
        [
            Subtask(description="producer", config={}, criticality="required"),
            Subtask(
                description="consumer",
                config={},
                criticality="required",
                depends_on=[0],
                dependency_failure_policy=policy,
            ),
        ]
    )


def test_failed_producer_does_not_stall_parent_when_consumer_blocked() -> None:
    # Given: a hard chain where the consumer only runs after the producer succeeds
    parent, children = _chain()

    # When: the producer fails; the blocked consumer can never report, no budget
    parent.handle_child_failure(children[0], "producer failed", max_redecompositions=0)

    # Then: the parent reaches a terminal decision instead of stalling in WAITING
    assert parent.status == AgentStatus.FAILED


def test_failed_producer_triggers_replan_when_budget_remains() -> None:
    # Given: the same hard chain, with re-plan budget
    parent, children = _chain()

    # When: the producer fails and the consumer is blocked
    parent.handle_child_failure(children[0], "producer failed", max_redecompositions=1)

    # Then: the parent re-decomposes (informed) instead of stalling
    assert parent.status == AgentStatus.ANALYZING
    assert parent.redecomposition_count == 1


def test_replan_policy_is_not_inert_and_triggers_replanning() -> None:
    # Given: a chain whose consumer declares the "replan" policy
    parent, children = _chain(policy="replan")

    # When: the producer fails with budget
    parent.handle_child_failure(children[0], "producer failed", max_redecompositions=1)

    # Then: replanning is triggered (the policy is no longer a no-op)
    assert parent.status == AgentStatus.ANALYZING
    assert parent.redecomposition_count == 1


def test_continue_policy_consumer_still_runs_so_parent_waits() -> None:
    # Given: a chain whose consumer tolerates a failed dependency and runs anyway
    parent, children = _chain(policy="continue")

    # When: the producer fails — the consumer is NOT blocked
    parent.handle_child_failure(children[0], "producer failed", max_redecompositions=0)

    # Then: the parent keeps waiting for the still-runnable consumer
    assert parent.status == AgentStatus.WAITING

    # And: once the consumer reports, the failed required producer fails the parent
    parent.handle_child_update(children[1], "consumer ran anyway")
    assert parent.status == AgentStatus.FAILED


def test_redecomposition_budget_is_symmetric_when_success_reports_last() -> None:
    # Given: two independent required children (no blocking edge)
    parent, children = _parent(
        [
            Subtask(description="A required", config={}, criticality="required"),
            Subtask(description="B required", config={}, criticality="required"),
        ]
    )

    # When: the required failure is recorded first, the success reports last
    parent.handle_child_failure(children[0], "A failed", max_redecompositions=1)
    assert parent.status == AgentStatus.WAITING  # still waiting for the independent B
    parent.handle_child_update(children[1], "B done", max_redecompositions=1)

    # Then: budget still drives a re-decomposition (not a hardcoded budget-0 failure)
    assert parent.status == AgentStatus.ANALYZING
    assert parent.redecomposition_count == 1
