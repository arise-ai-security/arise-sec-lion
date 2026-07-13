"""Tests for ParentNotificationService failure handling policies."""

from uuid import uuid4

import pytest

from bootstrap.tests.test_characterization import InMemoryEventStore
from core.application.services import AgentRepository, ParentNotificationService
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    ChildCompleted,
    ChildFailed,
    PhaseGateRecorded,
    RedecompositionTriggered,
    WorkCompleted,
    WorkFailed,
)
from core.domain.values.node_message import Briefing
from core.domain.values.subtask import Subtask


def _config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000},
        "tool": "claude_code",
    }


def _phase_manager() -> AgentSession:
    parent = AgentSession.create(agent_id=uuid4(), role=AgentRole.MANAGER, config=_config())
    parent.assign_task("[Review] verify")
    return parent


def test_phase_gate_not_recorded_while_redecomposing() -> None:
    # Given: a phase manager that is mid-flight (ANALYZING, e.g. re-decomposing),
    # not a terminal outcome
    parent = _phase_manager()
    child = AgentSession.create(
        agent_id=uuid4(), role=AgentRole.WORKER, config=_config(), parent_id=parent.agent_id
    )
    assert parent.status == AgentStatus.ANALYZING

    # When: the terminal-gate recorder runs
    ParentNotificationService._record_terminal_phase_gate(parent, child)

    # Then: no phase gate is recorded for a non-terminal parent
    assert not any(isinstance(e, PhaseGateRecorded) for e in parent.events)


def test_phase_gate_recorded_once_on_terminal_completion() -> None:
    # Given: a phase manager that terminally completes
    parent = _phase_manager()
    spawned = parent.apply_subtasks_and_spawn_children(
        subtasks=[Subtask(description="w", config=_config())],
        child_role=AgentRole.WORKER.value,
        briefing=parent.build_briefing_for_child(),
    )
    child_id, _ = spawned[0]
    parent.handle_child_update(child_id, "done")
    assert parent.status == AgentStatus.COMPLETED
    child = AgentSession.create(
        agent_id=child_id, role=AgentRole.WORKER, config=_config(), parent_id=parent.agent_id
    )

    # When: the terminal-gate recorder runs
    ParentNotificationService._record_terminal_phase_gate(parent, child)

    # Then: exactly one passing gate is recorded for the review phase
    gates = [e for e in parent.events if isinstance(e, PhaseGateRecorded)]
    assert len(gates) == 1
    assert gates[0].phase == "Review"
    assert gates[0].passed is True


async def _create_waiting_parent(
    repository: AgentRepository,
    role: AgentRole = AgentRole.MANAGER,
) -> tuple[AgentSession, AgentSession]:
    """Persist a parent in WAITING plus an infeasible child in FAILED."""
    parent = AgentSession.create(
        agent_id=uuid4(),
        role=role,
        config=_config(),
    )
    parent.assign_task("Main task")
    spawned = parent.apply_subtasks_and_spawn_children(
        subtasks=[Subtask(description="Subtask", config=_config())],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Main task"),
    )
    child_id, _ = spawned[0]
    await repository.save_new_agent(parent)

    child = AgentSession.create(
        agent_id=child_id,
        role=AgentRole.PENDING,
        config=_config(),
        parent_id=parent.agent_id,
    )
    child.assign_task("Subtask")
    child.mark_infeasible("No viable plan")
    await repository.save_new_agent(child)
    return parent, child


async def _waiting_parent_with_children(
    repository: AgentRepository,
    count: int = 2,
    role: AgentRole = AgentRole.MANAGER,
) -> tuple[AgentSession, list]:
    """Persist a parent in WAITING with spawned children."""
    parent = AgentSession.create(
        agent_id=uuid4(),
        role=role,
        config=_config(),
    )
    parent.assign_task("Main task")
    spawned = parent.apply_subtasks_and_spawn_children(
        subtasks=[
            Subtask(description=f"Subtask {i}", config=_config()) for i in range(count)
        ],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Main task"),
    )
    await repository.save_new_agent(parent)
    return parent, [child_id for child_id, _ in spawned]


def _make_failed_child(
    child_id, parent_id, task: str, reason: str, digest: str | None = None
) -> AgentSession:
    """Build a hard-failed WORKER carrying task/reason and an optional digest."""
    child = AgentSession.create(
        agent_id=child_id,
        role=AgentRole.WORKER,
        config=_config(),
        parent_id=parent_id,
    )
    child.assign_task(task)
    child.fail_with_reason(reason)
    if digest is not None:
        child.record_failure_digest(digest, source="worker_crash")
    return child


@pytest.mark.asyncio
async def test_infeasible_child_triggers_redecomposition_within_limit() -> None:
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(
        repository=repository,
        max_redecompositions=1,
    )

    parent, child = await _create_waiting_parent(repository)

    await notifier.notify_if_failed(child)

    reloaded_parent = await repository.load(parent.agent_id)
    assert reloaded_parent.status == AgentStatus.ANALYZING
    assert reloaded_parent.redecomposition_count == 1
    assert reloaded_parent.child_ids == []


@pytest.mark.asyncio
async def test_infeasible_child_propagates_failure_after_limit_exhausted() -> None:
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(
        repository=repository,
        max_redecompositions=1,
    )

    parent, child = await _create_waiting_parent(repository)
    await notifier.notify_if_failed(child)

    reloaded_parent = await repository.load(parent.agent_id)
    spawned = reloaded_parent.apply_subtasks_and_spawn_children(
        subtasks=[Subtask(description="Retry subtask", config=_config())],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Main task"),
    )
    retry_child_id, _ = spawned[0]
    await repository.persist_events(reloaded_parent)

    retry_child = AgentSession.create(
        agent_id=retry_child_id,
        role=AgentRole.PENDING,
        config=_config(),
        parent_id=parent.agent_id,
    )
    retry_child.assign_task("Retry subtask")
    retry_child.mark_infeasible("Still impossible")
    await repository.save_new_agent(retry_child)

    await notifier.notify_if_failed(retry_child)

    failed_parent = await repository.load(parent.agent_id)
    assert failed_parent.status == AgentStatus.FAILED
    assert failed_parent.redecomposition_count == 1
    assert "Redecomposition limit reached (1)" in (failed_parent.error_message or "")


@pytest.mark.asyncio
async def test_child_failure_passes_task_and_digest_to_parent_record() -> None:
    """A non-infeasible child failure records its task and digest on the parent."""
    # Given: a Manager in WAITING with two children, one hard-failing with a digest
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(repository=repository, max_redecompositions=2)

    parent, child_ids = await _waiting_parent_with_children(repository, count=2)
    child = _make_failed_child(
        child_ids[0],
        parent.agent_id,
        task="build the artifact",
        reason="Segfault in parser",
        digest="FAILURE: Segfault in parser\nLAST TOOL CALLS:\n[bash/tool_result] make",
    )
    await repository.save_new_agent(child)

    # When: the failure is notified upward
    await notifier.notify_if_failed(child)

    # Then: the parent's ChildFailureRecord retains task, reason, and digest
    reloaded_parent = await repository.load(parent.agent_id)
    record = reloaded_parent.failed_children[child_ids[0]]
    assert record.child_task == "build the artifact"
    assert record.reason == "Segfault in parser"
    assert record.digest is not None and "LAST TOOL CALLS" in record.digest
    # And: the second child has not reported, so the parent stays WAITING
    assert reloaded_parent.status == AgentStatus.WAITING


@pytest.mark.asyncio
async def test_all_children_failed_with_budget_redecomposes_not_fails() -> None:
    """When every child fails and redecomposition budget remains, the parent
    re-decomposes (ANALYZING) rather than failing."""
    # Given: a Manager in WAITING with two children and a redecomposition budget
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(repository=repository, max_redecompositions=2)

    parent, child_ids = await _waiting_parent_with_children(repository, count=2)

    # When: both children hard-fail and are notified upward
    for idx, child_id in enumerate(child_ids):
        child = _make_failed_child(
            child_id, parent.agent_id, task=f"Subtask {idx}", reason=f"boom {idx}"
        )
        await repository.save_new_agent(child)
        await notifier.notify_if_failed(child)

    # Then: the parent is ANALYZING via RedecompositionTriggered, not FAILED
    reloaded_parent = await repository.load(parent.agent_id)
    assert reloaded_parent.status == AgentStatus.ANALYZING
    assert reloaded_parent.redecomposition_count == 1
    parent_events = event_store.events_for(parent.agent_id)
    assert any(isinstance(e, RedecompositionTriggered) for e in parent_events)
    assert not any(isinstance(e, WorkFailed) for e in parent_events)


@pytest.mark.asyncio
async def test_infeasible_fast_path_preserves_failure_for_redecomposition() -> None:
    """An infeasible child is retained as Manager recovery evidence."""
    # Given: a Manager in WAITING with a single infeasible child and budget left
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(repository=repository, max_redecompositions=1)

    parent, child = await _create_waiting_parent(repository)

    # When: the infeasible failure is notified upward
    await notifier.notify_if_failed(child)

    # Then: the parent re-decomposes with the failed task and reason in history
    reloaded_parent = await repository.load(parent.agent_id)
    assert reloaded_parent.status == AgentStatus.ANALYZING
    assert len(reloaded_parent.failure_history) == 1
    assert reloaded_parent.failure_history[0].child_task == child.task_description
    assert reloaded_parent.failure_history[0].reason.startswith("Infeasible:")
    parent_events = event_store.events_for(parent.agent_id)
    assert any(isinstance(e, RedecompositionTriggered) for e in parent_events)
    assert any(isinstance(e, ChildFailed) for e in parent_events)


@pytest.mark.asyncio
async def test_non_manager_parent_never_redecomposes_after_child_failure() -> None:
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(repository=repository, max_redecompositions=2)
    parent, child = await _create_waiting_parent(repository, role=AgentRole.BOSS)

    await notifier.notify_if_failed(child)

    reloaded_parent = await repository.load(parent.agent_id)
    assert reloaded_parent.status == AgentStatus.FAILED
    assert reloaded_parent.redecomposition_count == 0
    parent_events = event_store.events_for(parent.agent_id)
    assert not any(isinstance(event, RedecompositionTriggered) for event in parent_events)


def _emit_work_completed(worker: AgentSession, result: str) -> None:
    """Drive a worker to COMPLETED via WorkCompleted (test helper)."""
    event = WorkCompleted(
        aggregate_id=worker.agent_id,
        sequence_number=worker._next_sequence(),
        result=result,
    )
    worker._emit(event)


async def _build_three_level_tree(
    repository: AgentRepository,
) -> tuple[AgentSession, AgentSession, AgentSession, AgentSession]:
    """BOSS → MANAGER → 2x WORKER. Workers already COMPLETED in storage.

    Returns (boss, manager, worker1, worker2).
    """
    # Given: build a three-level tree
    boss = AgentSession.create(
        agent_id=uuid4(),
        role=AgentRole.BOSS,
        config=_config(),
    )
    boss.assign_task("Top task")
    boss_spawned = boss.apply_subtasks_and_spawn_children(
        subtasks=[Subtask(description="Sub", config=_config())],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Top"),
    )
    manager_id, _ = boss_spawned[0]
    await repository.save_new_agent(boss)

    manager = AgentSession.create(
        agent_id=manager_id,
        role=AgentRole.MANAGER,
        config=_config(),
        parent_id=boss.agent_id,
    )
    manager.assign_task("Sub")
    manager_spawned = manager.apply_subtasks_and_spawn_children(
        subtasks=[
            Subtask(description="Leaf-A", config=_config()),
            Subtask(description="Leaf-B", config=_config()),
        ],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Sub"),
    )
    worker1_id, _ = manager_spawned[0]
    worker2_id, _ = manager_spawned[1]
    await repository.save_new_agent(manager)

    worker1 = AgentSession.create(
        agent_id=worker1_id,
        role=AgentRole.WORKER,
        config=_config(),
        parent_id=manager.agent_id,
    )
    worker1.assign_task("Leaf-A")
    worker1.start_worker_execution("claude_code")
    _emit_work_completed(worker1, "A done")
    await repository.save_new_agent(worker1)

    worker2 = AgentSession.create(
        agent_id=worker2_id,
        role=AgentRole.WORKER,
        config=_config(),
        parent_id=manager.agent_id,
    )
    worker2.assign_task("Leaf-B")
    worker2.start_worker_execution("claude_code")
    _emit_work_completed(worker2, "B done")
    await repository.save_new_agent(worker2)

    return boss, manager, worker1, worker2


@pytest.mark.asyncio
async def test_walks_upward_on_completed_terminal_parent() -> None:
    """When a sibling already drove the parent COMPLETED, the recursive
    notify_if_complete walk must continue upward to the grandparent.

    Repros the audit-1 Q4 OCC retry hang: WORKER2 terminal arrives after
    a racing sibling has already driven MANAGER → COMPLETED out-of-band.
    Without the upward walk, BOSS never sees ChildCompleted(MANAGER).
    """
    # Given: BOSS → MANAGER → (WORKER1, WORKER2). Manager is driven to
    # COMPLETED out-of-band before the second worker's notification fires.
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(repository=repository)
    boss, manager, _worker1, worker2 = await _build_three_level_tree(repository)

    racing_manager = await repository.load(manager.agent_id)
    racing_manager.handle_child_update(child_id=_worker1.agent_id, result="A done")
    racing_manager.handle_child_update(child_id=worker2.agent_id, result="B done")
    await repository.persist_events(racing_manager)

    racing_manager_after = await repository.load(manager.agent_id)
    assert racing_manager_after.status == AgentStatus.COMPLETED

    # When: the lost notification arrives anyway — under the old code
    # this would early-return on parent.status != WAITING and BOSS never
    # gets notified.
    reloaded_w2 = await repository.load(worker2.agent_id)
    await notifier.notify_if_complete(reloaded_w2)

    # Then: BOSS reaches COMPLETED via the upward walk.
    reloaded_boss = await repository.load(boss.agent_id)
    boss_events = await event_store.get_events(boss.agent_id)
    boss_child_completed = [
        e for e in boss_events
        if isinstance(e, ChildCompleted) and e.child_id == manager.agent_id
    ]
    assert len(boss_child_completed) == 1
    assert reloaded_boss.status == AgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_no_duplicate_on_already_notified_grandparent() -> None:
    """When two sibling paths both walk upward to the same grandparent,
    only one ChildCompleted(MANAGER) lands on the BOSS event log.

    The recursive walk on the COMPLETED branch is protected by the
    aggregate's idempotence guard.
    """
    # Given: BOSS → MANAGER → (WORKER1, WORKER2). The first sibling
    # path already drove MANAGER → COMPLETED and notified BOSS.
    event_store = InMemoryEventStore()
    repository = AgentRepository(event_store=event_store, max_retries=3)
    notifier = ParentNotificationService(repository=repository)
    boss, manager, worker1, worker2 = await _build_three_level_tree(repository)

    reloaded_w1 = await repository.load(worker1.agent_id)
    await notifier.notify_if_complete(reloaded_w1)
    reloaded_w2 = await repository.load(worker2.agent_id)
    await notifier.notify_if_complete(reloaded_w2)

    # When: a duplicate notification arrives (e.g. via OCC retry on the
    # original WORKER2 path) — the manager is COMPLETED, so the notifier
    # walks upward and the grandparent's idempotence guard suppresses the
    # second ChildCompleted(MANAGER).
    reloaded_w2_again = await repository.load(worker2.agent_id)
    await notifier.notify_if_complete(reloaded_w2_again)

    # Then: BOSS event log has exactly one ChildCompleted(MANAGER).
    boss_events = await event_store.get_events(boss.agent_id)
    boss_child_completed = [
        e for e in boss_events
        if isinstance(e, ChildCompleted) and e.child_id == manager.agent_id
    ]
    assert len(boss_child_completed) == 1
