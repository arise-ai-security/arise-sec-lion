"""Tests for HierarchyCollector."""

from datetime import timedelta

import pytest

from core.domain.events.events import (
    AgentCreated,
    ChildSpawned,
    TaskAssigned,
    WorkCompleted,
)
from core.domain.values.subtask import Subtask
from core.query.projections.hierarchy_collector import HierarchyCollector

from .conftest import (
    BASE_TIME,
    BOSS_ID,
    MANAGER_ID,
    WORKER_ID,
)


def _subtask_config() -> dict:
    """Standard config for test subtasks."""
    return {"strategy": "heuristic", "base": {"model": "test"}}


class TestHierarchyCollector:
    """Tests for HierarchyCollector."""

    @pytest.fixture
    def boss_events(self) -> list:
        """Events for BOSS agent."""
        return [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                parent_id=None,
                occurred_at=BASE_TIME,
            ),
            TaskAssigned(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                task_description="Main task",
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
            ChildSpawned(
                aggregate_id=BOSS_ID,
                sequence_number=3,
                child_id=MANAGER_ID,
                child_role="PENDING",
                subtask=Subtask(
                    description="Subtask 1",
                    config=_subtask_config(),
                ),
                child_config={},
                occurred_at=BASE_TIME + timedelta(seconds=2),
            ),
        ]

    @pytest.fixture
    def manager_events(self) -> list:
        """Events for MANAGER agent."""
        return [
            AgentCreated(
                aggregate_id=MANAGER_ID,
                sequence_number=1,
                role="MANAGER",
                parent_id=BOSS_ID,
                occurred_at=BASE_TIME + timedelta(seconds=3),
            ),
            ChildSpawned(
                aggregate_id=MANAGER_ID,
                sequence_number=2,
                child_id=WORKER_ID,
                child_role="PENDING",
                subtask=Subtask(
                    description="Worker subtask",
                    config=_subtask_config(),
                ),
                child_config={},
                occurred_at=BASE_TIME + timedelta(seconds=4),
            ),
        ]

    @pytest.fixture
    def worker_events(self) -> list:
        """Events for WORKER agent."""
        return [
            AgentCreated(
                aggregate_id=WORKER_ID,
                sequence_number=1,
                role="WORKER",
                parent_id=MANAGER_ID,
                occurred_at=BASE_TIME + timedelta(seconds=5),
            ),
            WorkCompleted(
                aggregate_id=WORKER_ID,
                sequence_number=2,
                result="Task done",
                occurred_at=BASE_TIME + timedelta(seconds=6),
            ),
        ]

    @pytest.mark.asyncio
    async def test_collect_single_agent(self, fake_event_store) -> None:
        """Should collect events from single agent with no children."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
        ]
        fake_event_store.add_events(BOSS_ID, events)

        collector = HierarchyCollector(fake_event_store)
        result = await collector.collect(BOSS_ID)

        assert len(result) == 1
        assert result[0].aggregate_id == BOSS_ID

    @pytest.mark.asyncio
    async def test_collect_parent_and_child(
        self, fake_event_store, boss_events, manager_events
    ) -> None:
        """Should collect events from parent and child agents."""
        fake_event_store.add_events(BOSS_ID, boss_events)
        fake_event_store.add_events(MANAGER_ID, manager_events)

        collector = HierarchyCollector(fake_event_store)
        result = await collector.collect(BOSS_ID)

        # Should have events from both BOSS and MANAGER
        boss_count = sum(1 for e in result if e.aggregate_id == BOSS_ID)
        manager_count = sum(1 for e in result if e.aggregate_id == MANAGER_ID)

        assert boss_count == 3
        assert manager_count == 2

    @pytest.mark.asyncio
    async def test_collect_full_hierarchy(
        self, fake_event_store, boss_events, manager_events, worker_events
    ) -> None:
        """Should collect events from entire hierarchy (3 levels)."""
        fake_event_store.add_events(BOSS_ID, boss_events)
        fake_event_store.add_events(MANAGER_ID, manager_events)
        fake_event_store.add_events(WORKER_ID, worker_events)

        collector = HierarchyCollector(fake_event_store)
        result = await collector.collect(BOSS_ID)

        # Should have events from all 3 agents
        agent_ids = {e.aggregate_id for e in result}
        assert agent_ids == {BOSS_ID, MANAGER_ID, WORKER_ID}

    @pytest.mark.asyncio
    async def test_collect_returns_sorted_by_timestamp(
        self, fake_event_store, boss_events, manager_events, worker_events
    ) -> None:
        """Should return events sorted by occurred_at."""
        fake_event_store.add_events(BOSS_ID, boss_events)
        fake_event_store.add_events(MANAGER_ID, manager_events)
        fake_event_store.add_events(WORKER_ID, worker_events)

        collector = HierarchyCollector(fake_event_store)
        result = await collector.collect(BOSS_ID)

        timestamps = [e.occurred_at for e in result]
        assert timestamps == sorted(timestamps)

    @pytest.mark.asyncio
    async def test_collect_handles_empty_agent(self, fake_event_store) -> None:
        """Should handle agent with no events gracefully."""
        fake_event_store.add_events(BOSS_ID, [])

        collector = HierarchyCollector(fake_event_store)
        result = await collector.collect(BOSS_ID)

        assert result == []

    @pytest.mark.asyncio
    async def test_collect_avoids_cycles(self, fake_event_store) -> None:
        """Should handle potential cycles without infinite loop."""
        # Create events where BOSS spawns MANAGER, and we'll add
        # BOSS_ID to visited set before processing
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
            ChildSpawned(
                aggregate_id=BOSS_ID,
                sequence_number=2,
                child_id=BOSS_ID,  # Spawn self (pathological case)
                child_role="PENDING",
                subtask=Subtask(description="Self", config=_subtask_config()),
                child_config={},
                occurred_at=BASE_TIME + timedelta(seconds=1),
            ),
        ]
        fake_event_store.add_events(BOSS_ID, events)

        collector = HierarchyCollector(fake_event_store)
        # Should complete without infinite loop
        result = await collector.collect(BOSS_ID)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_collect_agent_ids(
        self, fake_event_store, boss_events, manager_events, worker_events
    ) -> None:
        """Should collect just agent IDs in hierarchy."""
        fake_event_store.add_events(BOSS_ID, boss_events)
        fake_event_store.add_events(MANAGER_ID, manager_events)
        fake_event_store.add_events(WORKER_ID, worker_events)

        collector = HierarchyCollector(fake_event_store)
        result = await collector.collect_agent_ids(BOSS_ID)

        assert result == {BOSS_ID, MANAGER_ID, WORKER_ID}

    @pytest.mark.asyncio
    async def test_get_hierarchy_depth_single_agent(self, fake_event_store) -> None:
        """Depth should be 0 for single agent."""
        events = [
            AgentCreated(
                aggregate_id=BOSS_ID,
                sequence_number=1,
                role="BOSS",
                occurred_at=BASE_TIME,
            ),
        ]
        fake_event_store.add_events(BOSS_ID, events)

        collector = HierarchyCollector(fake_event_store)
        depth = await collector.get_hierarchy_depth(BOSS_ID)

        assert depth == 0

    @pytest.mark.asyncio
    async def test_get_hierarchy_depth_with_children(
        self, fake_event_store, boss_events, manager_events, worker_events
    ) -> None:
        """Depth should reflect deepest level."""
        fake_event_store.add_events(BOSS_ID, boss_events)
        fake_event_store.add_events(MANAGER_ID, manager_events)
        fake_event_store.add_events(WORKER_ID, worker_events)

        collector = HierarchyCollector(fake_event_store)
        depth = await collector.get_hierarchy_depth(BOSS_ID)

        # BOSS (0) -> MANAGER (1) -> WORKER (2)
        assert depth == 2
