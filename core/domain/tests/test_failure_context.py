"""Failure-context retention tests: digests, child failure records, informed redecomposition."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.events.events import (
    ChildFailed,
    FailureDigestRecorded,
    RedecompositionTriggered,
    ThoughtCaptured,
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


def _build_waiting_parent_with_children(
    count: int = 2,
    criticalities: list[str] | None = None,
) -> tuple[AgentSession, list]:
    parent = AgentSession.create(agent_id=uuid4(), role=AgentRole.BOSS, config=_config())
    parent.assign_task("Top task")
    spawned = parent.apply_subtasks_and_spawn_children(
        subtasks=[
            Subtask(
                description=f"subtask-{i}",
                config=_config(),
                criticality=(criticalities or ["required"] * count)[i],
            )
            for i in range(count)
        ],
        child_role=AgentRole.PENDING.value,
        briefing=Briefing.simple("Top task"),
    )
    return parent, [child_id for child_id, _ in spawned]


def test_child_failure_record_retains_context() -> None:
    """ChildFailed must retain task, reason, and digest on the parent."""

    # Given: a parent in WAITING with two children
    parent, child_ids = _build_waiting_parent_with_children()

    # When: one child fails with full context
    parent.handle_child_failure(
        child_id=child_ids[0],
        reason="TimeoutError: exec timed out",
        child_task="build the artifact",
        digest="FAILURE: timeout\nLAST TOOL CALLS:\n[bash] make",
    )

    # Then: the record keeps every field
    record = parent.failed_children[child_ids[0]]
    assert record.child_task == "build the artifact"
    assert record.reason == "TimeoutError: exec timed out"
    assert record.digest is not None and "LAST TOOL CALLS" in record.digest


def test_legacy_child_failed_event_replays_without_new_fields() -> None:
    """Pre-enrichment ChildFailed rows (no child_task/digest) must replay cleanly."""

    # Given: a parent whose history contains a legacy-shaped ChildFailed
    parent, child_ids = _build_waiting_parent_with_children()
    legacy = ChildFailed(
        aggregate_id=parent.agent_id,
        sequence_number=parent.version + 1,
        child_id=child_ids[0],
        reason="boom",
    )

    # When: the event is applied (as replay would)
    parent._apply(legacy)

    # Then: the record defaults the enrichment fields
    record = parent.failed_children[child_ids[0]]
    assert record.child_task == ""
    assert record.digest is None
    assert record.reason == "boom"


def test_all_children_failed_triggers_informed_redecomposition() -> None:
    """With budget left, all-children-failed re-decomposes instead of failing."""

    # Given: a parent in WAITING with two children
    parent, child_ids = _build_waiting_parent_with_children()

    # When: both children fail and redecomposition budget remains
    parent.handle_child_failure(
        child_id=child_ids[0], reason="segfault", child_task="task A", max_redecompositions=2
    )
    parent.handle_child_failure(
        child_id=child_ids[1], reason="OOM", child_task="task B", max_redecompositions=2
    )

    # Then: the parent re-decomposes with a per-child failure summary
    redecomps = [e for e in parent.events if isinstance(e, RedecompositionTriggered)]
    assert len(redecomps) == 1
    assert "task A: segfault" in redecomps[0].reason
    assert "task B: OOM" in redecomps[0].reason
    assert not any(isinstance(e, WorkFailed) for e in parent.events)
    assert parent.status == AgentStatus.ANALYZING

    # And: the failure history was snapshotted before children were cleared
    assert [r.child_task for r in parent.failure_history] == ["task A", "task B"]
    assert parent.failed_children == {}


def test_all_children_failed_without_budget_fails_with_reasons() -> None:
    """With no budget, all-children-failed emits WorkFailed listing each child."""

    # Given: a parent in WAITING with two children
    parent, child_ids = _build_waiting_parent_with_children()

    # When: both children fail and no redecomposition budget exists
    parent.handle_child_failure(child_id=child_ids[0], reason="segfault", child_task="task A")
    parent.handle_child_failure(child_id=child_ids[1], reason="OOM", child_task="task B")

    # Then: WorkFailed carries the per-child summary
    failed = [e for e in parent.events if isinstance(e, WorkFailed)]
    assert len(failed) == 1
    assert failed[0].reason.startswith("All 2 children failed:")
    assert "task A: segfault" in failed[0].reason
    assert "task B: OOM" in failed[0].reason


def test_partial_completion_note_lists_failure_reasons() -> None:
    """Partial success must annotate results with each failed child's reason."""

    # Given: a parent in WAITING with two children
    parent, child_ids = _build_waiting_parent_with_children(
        criticalities=["optional", "required"]
    )

    # When: one child fails (multi-line reason) and the other completes
    parent.handle_child_failure(
        child_id=child_ids[0],
        reason="RuntimeError: exec failed\nTraceback (most recent call last): ...",
        child_task="task A",
    )
    parent.handle_child_update(child_id=child_ids[1], result="done")

    # Then: the note carries the task label and the reason's first line only
    completed = [e for e in parent.events if isinstance(e, WorkCompleted)]
    assert len(completed) == 1
    assert "task A: RuntimeError: exec failed" in completed[0].result
    assert "Traceback" not in completed[0].result


def test_recent_thoughts_replay_parity_and_bounds() -> None:
    """recent_thoughts must be capped and identical between live apply and replay."""

    # Given: a worker that captured 25 tool outputs, one oversized
    worker = AgentSession.create(agent_id=uuid4(), role=AgentRole.WORKER, config=_config())
    worker.assign_task("do work")
    for i in range(25):
        worker.apply_worker_event(
            ThoughtCaptured(
                aggregate_id=worker.agent_id,
                sequence_number=0,
                content=f"output {i} " + ("x" * 600 if i == 24 else ""),
                output_type="tool_result",
                tool_name="bash",
            )
        )

    # When: the same history is replayed
    replayed = AgentSession.load_from_history(list(worker.events))

    # Then: only the newest 20 excerpts are retained, bounded to 500 chars
    assert len(worker.recent_thoughts) == 20
    assert worker.recent_thoughts[0].content.startswith("output 5")
    assert len(worker.recent_thoughts[-1].content) == 500

    # And: replay reconstructs the identical tail
    assert list(replayed.recent_thoughts) == list(worker.recent_thoughts)


def test_failure_digest_recorded_and_retained_across_retry() -> None:
    """The digest must survive RetryScheduled, mirroring verification_feedback."""

    # Given: a failed worker with a recorded digest
    worker = AgentSession.create(agent_id=uuid4(), role=AgentRole.WORKER, config=_config())
    worker.assign_task("do work")
    worker.fail_with_reason("adapter crash")
    worker.record_failure_digest("FAILURE: adapter crash", source="worker_crash")
    assert worker.failure_digest == "FAILURE: adapter crash"

    # When: a retry is scheduled and the history is replayed
    worker.schedule_retry(reason="adapter crash")
    replayed = AgentSession.load_from_history(list(worker.events))

    # Then: the digest is retained on both the live aggregate and the replay
    assert worker.failure_digest == "FAILURE: adapter crash"
    assert replayed.failure_digest == "FAILURE: adapter crash"
    assert replayed.error_message is None  # RetryScheduled still clears the error

    # And: the digest event round-trips its source
    digests = [e for e in worker.events if isinstance(e, FailureDigestRecorded)]
    assert len(digests) == 1 and digests[0].source == "worker_crash"
