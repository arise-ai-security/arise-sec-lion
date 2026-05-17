"""Unit tests for the pure transforms (no DB / no asyncpg required)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from experiments.shared.scripts.db.models import EventRow
from experiments.shared.scripts.db.transforms import (
    build_agent_trajectory,
    build_parent_trace,
    prettify_trajectory,
)


_BASE_TIME = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


def _row(
    aggregate_id: UUID,
    seq: int,
    event_type: str,
    payload: dict,
    *,
    offset_ms: int = 0,
) -> EventRow:
    return EventRow(
        event_id=uuid4(),
        aggregate_id=aggregate_id,
        sequence_number=seq,
        event_type=event_type,
        payload=payload,
        occurred_at=_BASE_TIME + timedelta(milliseconds=offset_ms),
        metadata={},
    )


# ---------------------------------------------------------------------------
# build_agent_trajectory
# ---------------------------------------------------------------------------


class TestBuildAgentTrajectory:
    def test_own_events_only_preserves_order(self) -> None:
        # Given: three events for one agent
        agent = uuid4()
        events = [
            _row(agent, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            _row(agent, 2, "TaskAssigned", {"task_description": "x"}, offset_ms=10),
            _row(agent, 3, "WorkCompleted", {"result": "ok"}, offset_ms=20),
        ]

        # When: build trajectory with no parent outcomes
        traj = build_agent_trajectory(events)

        # Then: events are returned in original (sequence_number) order
        assert [e.sequence_number for e in traj] == [1, 2, 3]
        assert all(e.aggregate_id == agent for e in traj)

    def test_parent_outcome_appended_after_self_terminal(self) -> None:
        # Given: child completes at t=20; parent emits ChildCompleted at t=21
        child = uuid4()
        parent = uuid4()
        own = [
            _row(child, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(parent)}, offset_ms=0),
            _row(child, 2, "WorkCompleted", {"result": "ok"}, offset_ms=20),
        ]
        parent_outcomes = [
            _row(parent, 50, "ChildCompleted",
                 {"child_id": str(child), "result": "ok"}, offset_ms=21),
        ]

        # When: combined into one trajectory
        traj = build_agent_trajectory(own, parent_outcomes)

        # Then: parent outcome lands AFTER the child's own WorkCompleted
        assert [e.event_type for e in traj] == [
            "AgentCreated", "WorkCompleted", "ChildCompleted",
        ]
        assert traj[-1].aggregate_id == parent

    def test_parent_outcome_stays_at_tail_on_tied_timestamp(self) -> None:
        # Given: child's WorkCompleted and parent's ChildCompleted share
        # the SAME microsecond. A naive merge-sort by occurred_at could
        # place the parent's event BEFORE the child's terminal —
        # impossible because the parent only notifies after observing.
        child = uuid4()
        parent = uuid4()
        own = [
            _row(child, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(parent)}, offset_ms=0),
            _row(child, 2, "WorkCompleted", {"result": "ok"}, offset_ms=100),
        ]
        parent_outcomes = [
            _row(parent, 30, "ChildCompleted",
                 {"child_id": str(child), "result": "ok"}, offset_ms=100),
        ]

        # When: built into a trajectory
        traj = build_agent_trajectory(own, parent_outcomes)

        # Then: parent's ChildCompleted is strictly the last event
        assert [e.event_type for e in traj] == [
            "AgentCreated", "WorkCompleted", "ChildCompleted",
        ]
        assert traj[-1].aggregate_id == parent


# ---------------------------------------------------------------------------
# build_parent_trace
# ---------------------------------------------------------------------------


class TestBuildParentTrace:
    def test_orders_globally_by_time(self) -> None:
        # Given: an ancestor chain (boss → manager → worker) with
        # interleaved events emitted out of insertion order.
        boss, mgr, worker = uuid4(), uuid4(), uuid4()
        events = [
            # boss starts
            _row(boss, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            _row(boss, 2, "ChildSpawned",
                 {"child_id": str(mgr), "child_role": "manager"}, offset_ms=5),
            # mgr starts
            _row(mgr, 1, "AgentCreated",
                 {"role": "manager", "parent_id": str(boss)}, offset_ms=6),
            _row(mgr, 2, "ChildSpawned",
                 {"child_id": str(worker), "child_role": "worker"}, offset_ms=10),
            # worker starts
            _row(worker, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(mgr)}, offset_ms=11),
            _row(worker, 2, "WorkCompleted", {"result": "ok"}, offset_ms=20),
        ]
        # Shuffle the insertion order — build_parent_trace must restore time order.
        shuffled = [events[3], events[0], events[5], events[1], events[4], events[2]]

        # When
        trace = build_parent_trace(shuffled)

        # Then: chronological order, regardless of which aggregate emitted
        assert [e.occurred_at for e in trace] == sorted(e.occurred_at for e in events)

    def test_lineage_with_single_node_just_orders_input(self) -> None:
        # Given: only the boss in the lineage (boss-as-target case)
        boss = uuid4()
        events = [
            _row(boss, 2, "WorkCompleted", {"result": "ok"}, offset_ms=20),
            _row(boss, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
        ]

        # When
        trace = build_parent_trace(events)

        # Then: time-sorted
        assert [e.sequence_number for e in trace] == [1, 2]

    def test_tied_timestamps_break_by_seq_then_aggregate(self) -> None:
        # Given: boss and worker each emit at the same microsecond.
        # sequence_number is per-aggregate (boss seq 5 vs worker seq 5
        # are unrelated), so the tertiary key (aggregate_id as text)
        # is what actually provides determinism.
        boss = UUID("00000000-0000-0000-0000-000000000001")
        worker = UUID("00000000-0000-0000-0000-000000000002")
        events = [
            _row(worker, 5, "ThoughtCaptured", {"tool_name": "x"}, offset_ms=50),
            _row(boss, 5, "ThoughtCaptured", {"tool_name": "y"}, offset_ms=50),
        ]

        # When
        trace = build_parent_trace(events)

        # Then: ordering is deterministic across runs — boss UUID sorts
        # before worker UUID lexicographically, so boss comes first.
        # (This documents the behavior; cross-aggregate causality on
        # microsecond ties is NOT guaranteed by the sort key — it relies
        # on the wall clock having ordered emissions correctly.)
        assert [e.aggregate_id for e in trace] == [boss, worker]


# ---------------------------------------------------------------------------
# prettify_trajectory
# ---------------------------------------------------------------------------


class TestPrettifyTrajectory:
    def test_empty_returns_empty(self) -> None:
        assert prettify_trajectory([]) == []

    def test_header_reports_role_and_event_count(self) -> None:
        # Given: a single-aggregate trajectory of two events
        agent = uuid4()
        events = [
            _row(agent, 1, "AgentCreated", {"role": "manager"}, offset_ms=0),
            _row(agent, 2, "WorkCompleted", {"result": "ok"}, offset_ms=10),
        ]

        # When: prettified
        lines = prettify_trajectory(events)

        # Then: header carries agent_id, role, and event count
        header = lines[0]
        assert "agent=" in header
        assert "role=manager" in header
        assert "events=2" in header

    def test_header_reports_aggregates_for_multi_agent(self) -> None:
        # Given: a 2-aggregate trace (e.g. lineage output)
        boss, worker = uuid4(), uuid4()
        events = [
            _row(boss, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            _row(worker, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(boss)}, offset_ms=5),
        ]

        # When
        lines = prettify_trajectory(events)

        # Then: header signals multi-aggregate, not a single agent_id
        header = lines[0]
        assert "aggregates=2" in header
        assert "events=2" in header

    def test_thought_runs_are_collapsed(self) -> None:
        # Given: a burst of 5 ThoughtCaptured between non-thought events
        agent = uuid4()
        events = [
            _row(agent, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            _row(agent, 2, "ThoughtCaptured", {"tool_name": "bash"}, offset_ms=1),
            _row(agent, 3, "ThoughtCaptured", {"tool_name": "read"}, offset_ms=2),
            _row(agent, 4, "ThoughtCaptured", {"tool_name": "bash"}, offset_ms=3),
            _row(agent, 5, "ThoughtCaptured", {"tool_name": "edit"}, offset_ms=4),
            _row(agent, 6, "ThoughtCaptured", {"tool_name": "bash"}, offset_ms=5),
            _row(agent, 7, "WorkCompleted", {"result": "ok"}, offset_ms=6),
        ]

        # When
        lines = prettify_trajectory(events)

        # Then: exactly one collapsed summary appears, with the seq
        # range, the count, and the unique tool list (sorted).
        thought_lines = [ln for ln in lines if "ThoughtCaptured" in ln]
        assert len(thought_lines) == 1
        assert "x5" in thought_lines[0]
        assert "seq=   2..6" in thought_lines[0]
        assert "tools: bash, edit, read" in thought_lines[0]

    def test_thought_runs_not_collapsed_when_disabled(self) -> None:
        # Given: 3 consecutive ThoughtCaptured rows
        agent = uuid4()
        events = [
            _row(agent, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            *[
                _row(agent, 1 + i, "ThoughtCaptured", {"tool_name": "x"},
                     offset_ms=i)
                for i in range(1, 4)
            ],
        ]

        # When
        lines = prettify_trajectory(events, collapse_thoughts=False)

        # Then: each ThoughtCaptured gets its own line
        thought_lines = [ln for ln in lines if "ThoughtCaptured" in ln]
        assert len(thought_lines) == 3
        assert all(" x" not in ln for ln in thought_lines)

    def test_collapse_flushes_on_aggregate_boundary(self) -> None:
        # Given: a thought run from agent A immediately followed by a
        # thought run from agent B (the lineage / cross-aggregate case).
        # Collapsing across the boundary would erase per-agent attribution.
        agent_a = uuid4()
        agent_b = uuid4()
        events = [
            _row(agent_a, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            _row(agent_a, 2, "ThoughtCaptured", {"tool_name": "bash"}, offset_ms=1),
            _row(agent_a, 3, "ThoughtCaptured", {"tool_name": "read"}, offset_ms=2),
            _row(agent_b, 1, "AgentCreated", {"role": "worker"}, offset_ms=3),
            _row(agent_b, 2, "ThoughtCaptured", {"tool_name": "edit"}, offset_ms=4),
            _row(agent_b, 3, "ThoughtCaptured", {"tool_name": "edit"}, offset_ms=5),
        ]

        # When
        lines = prettify_trajectory(events)

        # Then: TWO collapsed summary lines, one per aggregate
        thought_lines = [ln for ln in lines if "ThoughtCaptured" in ln]
        assert len(thought_lines) == 2
        assert all("x2" in ln for ln in thought_lines)
        # First summary carries agent_a's tools, second carries agent_b's
        assert "tools: bash, read" in thought_lines[0]
        assert "tools: edit" in thought_lines[1]

    def test_collapse_flushes_on_adjacent_cross_aggregate_thoughts(self) -> None:
        # Given: two ThoughtCaptured rows from DIFFERENT aggregates with
        # NO intervening non-thought event. Without an aggregate-boundary
        # flush these would fuse into one summary line and lose attribution.
        agent_a = uuid4()
        agent_b = uuid4()
        events = [
            _row(agent_a, 1, "ThoughtCaptured", {"tool_name": "bash"}, offset_ms=0),
            _row(agent_b, 1, "ThoughtCaptured", {"tool_name": "edit"}, offset_ms=1),
        ]

        # When
        lines = prettify_trajectory(events)

        # Then: two distinct ThoughtCaptured lines (single-event each,
        # so no "xN" suffix — but two separate per-aggregate lines)
        thought_lines = [ln for ln in lines if "ThoughtCaptured" in ln]
        assert len(thought_lines) == 2

    def test_long_string_payloads_are_truncated(self) -> None:
        # Given: a WorkCompleted with a very long result string
        agent = uuid4()
        long_result = "x" * 500
        events = [
            _row(agent, 1, "WorkCompleted",
                 {"result": long_result}, offset_ms=0),
        ]

        # When
        lines = prettify_trajectory(events)

        # Then: rendered line is short and ends with "..."
        worklines = [ln for ln in lines if "WorkCompleted" in ln]
        assert len(worklines) == 1
        assert len(worklines[0]) < 200
        assert "..." in worklines[0]


# ---------------------------------------------------------------------------
# Synthetic hierarchy — multi-agent (the only place the
# build_agent_trajectory + build_parent_trace cross-aggregate semantics
# are exercised end-to-end through the pure transform layer)
# ---------------------------------------------------------------------------


class TestSyntheticHierarchy:
    def test_worker_trajectory_includes_managers_childcompleted(self) -> None:
        # Given: boss → manager → worker; worker finishes, mgr emits ChildCompleted
        boss, manager, worker = uuid4(), uuid4(), uuid4()
        worker_own = [
            _row(worker, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(manager)}, offset_ms=0),
            _row(worker, 2, "TaskAssigned",
                 {"task_description": "fix bug"}, offset_ms=1),
            _row(worker, 3, "WorkCompleted", {"result": "patched"}, offset_ms=10),
        ]
        # Manager only contributes its ChildCompleted for the worker.
        manager_outcome_for_worker = [
            _row(manager, 12, "ChildCompleted",
                 {"child_id": str(worker), "result": "patched"}, offset_ms=11),
        ]
        _ = boss  # part of the conceptual setup, not in the trajectory

        # When
        traj = build_agent_trajectory(worker_own, manager_outcome_for_worker)

        # Then: documented shape
        # AgentCreated → TaskAssigned → WorkCompleted → ChildCompleted(parent)
        types = [e.event_type for e in traj]
        assert types == [
            "AgentCreated", "TaskAssigned", "WorkCompleted", "ChildCompleted",
        ]
        # Parent-outcome row owned by manager
        assert traj[-1].aggregate_id == manager
        # All earlier events are the worker's own
        assert all(e.aggregate_id == worker for e in traj[:-1])

    def test_failed_worker_trajectory_uses_childfailed(self) -> None:
        manager, worker = uuid4(), uuid4()
        worker_own = [
            _row(worker, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(manager)}, offset_ms=0),
            _row(worker, 2, "WorkFailed", {"reason": "timeout"}, offset_ms=5),
        ]
        outcomes = [
            _row(manager, 30, "ChildFailed",
                 {"child_id": str(worker), "reason": "timeout"}, offset_ms=6),
        ]

        traj = build_agent_trajectory(worker_own, outcomes)
        lines = prettify_trajectory(traj)

        assert traj[-1].event_type == "ChildFailed"
        assert any("ChildFailed" in ln and "reason=" in ln for ln in lines)

    def test_parent_trace_orders_three_aggregates_chronologically(self) -> None:
        # Given: 3-level lineage with each ancestor emitting AgentCreated +
        # ChildSpawned, terminal worker completing
        boss, manager, worker = uuid4(), uuid4(), uuid4()
        events = [
            _row(boss, 1, "AgentCreated", {"role": "boss"}, offset_ms=0),
            _row(boss, 2, "ChildSpawned",
                 {"child_id": str(manager), "child_role": "manager"}, offset_ms=10),
            _row(manager, 1, "AgentCreated",
                 {"role": "manager", "parent_id": str(boss)}, offset_ms=11),
            _row(manager, 2, "ChildSpawned",
                 {"child_id": str(worker), "child_role": "worker"}, offset_ms=20),
            _row(worker, 1, "AgentCreated",
                 {"role": "worker", "parent_id": str(manager)}, offset_ms=21),
            _row(worker, 2, "WorkCompleted", {"result": "ok"}, offset_ms=30),
        ]

        # When
        trace = build_parent_trace(events)

        # Then: full chronology, one aggregate transitioning to the next
        assert [e.aggregate_id for e in trace] == [
            boss, boss, manager, manager, worker, worker,
        ]
