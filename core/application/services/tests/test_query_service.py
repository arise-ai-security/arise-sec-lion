"""Tests for QueryService left-to-right worker execution guarantee.

The QueryService guarantees left-to-right worker execution through:
1. Hierarchical path computation - tuple paths from root to each agent
2. Left sibling blocking check - ensures no incomplete work to the left
3. Sequential worker filtering - only returns the leftmost eligible worker
"""

import pytest
from unittest.mock import AsyncMock
from uuid import uuid4

from core.application.services.query_service import QueryService
from core.domain.events import (
    AgentCreated,
    ChildSpawned,
    ComplexityEvaluated,
    TaskAssigned,
    WorkCompleted,
)
from core.domain.model import AgentRole, AgentStatus
from core.domain.subtask import Subtask


def _test_config() -> dict:
    """Create a minimal test config."""
    return {
        "base": {"model": "gpt-4", "temperature": 0.7, "max_tokens": 1000},
        "strategy": "heuristic",
        "tool": "claude_code",
    }


def _create_boss_events(boss_id, child_ids=None) -> list:
    """Create events for a BOSS agent with optional children."""
    child_ids = child_ids or []
    events = [
        AgentCreated(
            aggregate_id=boss_id,
            sequence_number=1,
            role=AgentRole.BOSS.value,
            parent_id=None,
            config=_test_config(),
            tree_sequence_id=0,
        ),
        TaskAssigned(
            aggregate_id=boss_id,
            sequence_number=2,
            task_description="Main task",
        ),
    ]

    seq = 3
    for idx, child_id in enumerate(child_ids):
        events.append(
            ChildSpawned(
                aggregate_id=boss_id,
                sequence_number=seq,
                child_id=child_id,
                child_role=AgentRole.PENDING.value,
                subtask=Subtask(description=f"Subtask {idx}", config=_test_config()),
                child_config=_test_config(),
            )
        )
        seq += 1

    return events


def _create_worker_events(worker_id, parent_id, sibling_index=0, completed=False) -> list:
    """Create events for a WORKER agent."""
    events = [
        AgentCreated(
            aggregate_id=worker_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=parent_id,
            config=_test_config(),
            tree_sequence_id=sibling_index,
        ),
        TaskAssigned(
            aggregate_id=worker_id,
            sequence_number=2,
            task_description=f"Worker task {sibling_index}",
        ),
        ComplexityEvaluated(
            aggregate_id=worker_id,
            sequence_number=3,
            complexity="simple",
            determined_role=AgentRole.WORKER.value,
        ),
    ]

    if completed:
        events.append(
            WorkCompleted(
                aggregate_id=worker_id,
                sequence_number=4,
                result="Done",
            )
        )

    return events


def _create_manager_events(manager_id, parent_id, child_ids=None, sibling_index=0, completed=False) -> list:
    """Create events for a MANAGER agent with optional children."""
    child_ids = child_ids or []
    events = [
        AgentCreated(
            aggregate_id=manager_id,
            sequence_number=1,
            role=AgentRole.PENDING.value,
            parent_id=parent_id,
            config=_test_config(),
            tree_sequence_id=sibling_index,
        ),
        TaskAssigned(
            aggregate_id=manager_id,
            sequence_number=2,
            task_description=f"Manager task {sibling_index}",
        ),
        ComplexityEvaluated(
            aggregate_id=manager_id,
            sequence_number=3,
            complexity="complex",
            determined_role=AgentRole.MANAGER.value,
        ),
    ]

    seq = 4
    for idx, child_id in enumerate(child_ids):
        events.append(
            ChildSpawned(
                aggregate_id=manager_id,
                sequence_number=seq,
                child_id=child_id,
                child_role=AgentRole.PENDING.value,
                subtask=Subtask(description=f"Sub-subtask {idx}", config=_test_config()),
                child_config=_test_config(),
            )
        )
        seq += 1

    if completed:
        events.append(
            WorkCompleted(
                aggregate_id=manager_id,
                sequence_number=seq,
                result="Done",
            )
        )

    return events


@pytest.fixture
def mock_event_store():
    """Create a mock event store."""
    store = AsyncMock()
    store.get_events = AsyncMock(return_value=[])
    return store


@pytest.fixture
def query_service(mock_event_store):
    """Create a QueryService with mock event store."""
    return QueryService(mock_event_store)


@pytest.mark.asyncio
async def test_hierarchical_path_computation_simple_tree(query_service, mock_event_store):
    """Test that hierarchical paths are computed correctly for a simple tree.

    Tree structure:
        BOSS (0,)
        ├── Worker1 (0, 0)
        └── Worker2 (0, 1)
    """
    boss_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [worker1_id, worker2_id])
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, boss_id, sibling_index=0)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, boss_id, sibling_index=1)
        return []

    mock_event_store.get_events.side_effect = get_events

    # Build hierarchy
    await query_service._build_hierarchy_data(boss_id)

    # Verify paths
    boss_path = query_service._compute_hierarchical_path(boss_id)
    worker1_path = query_service._compute_hierarchical_path(worker1_id)
    worker2_path = query_service._compute_hierarchical_path(worker2_id)

    assert boss_path == (0,)
    assert worker1_path == (0, 0)
    assert worker2_path == (0, 1)

    # Verify lexicographic ordering matches left-to-right
    assert boss_path < worker1_path < worker2_path


@pytest.mark.asyncio
async def test_hierarchical_path_computation_nested_tree(query_service, mock_event_store):
    """Test hierarchical paths for a deeper nested tree.

    Tree structure:
        BOSS (0,)
        ├── Manager1 (0, 0)
        │   ├── Worker1 (0, 0, 0)
        │   └── Worker2 (0, 0, 1)
        └── Manager2 (0, 1)
            └── Worker3 (0, 1, 0)
    """
    boss_id = uuid4()
    manager1_id = uuid4()
    manager2_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()
    worker3_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [manager1_id, manager2_id])
        elif agent_id == manager1_id:
            return _create_manager_events(manager1_id, boss_id, [worker1_id, worker2_id], sibling_index=0)
        elif agent_id == manager2_id:
            return _create_manager_events(manager2_id, boss_id, [worker3_id], sibling_index=1)
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, manager1_id, sibling_index=0)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, manager1_id, sibling_index=1)
        elif agent_id == worker3_id:
            return _create_worker_events(worker3_id, manager2_id, sibling_index=0)
        return []

    mock_event_store.get_events.side_effect = get_events

    await query_service._build_hierarchy_data(boss_id)

    # Verify paths
    assert query_service._compute_hierarchical_path(worker1_id) == (0, 0, 0)
    assert query_service._compute_hierarchical_path(worker2_id) == (0, 0, 1)
    assert query_service._compute_hierarchical_path(worker3_id) == (0, 1, 0)

    # Verify left-to-right order
    w1_path = query_service._compute_hierarchical_path(worker1_id)
    w2_path = query_service._compute_hierarchical_path(worker2_id)
    w3_path = query_service._compute_hierarchical_path(worker3_id)
    assert w1_path < w2_path < w3_path


@pytest.mark.asyncio
async def test_left_sibling_blocking_basic(query_service, mock_event_store):
    """Test that a worker is blocked by incomplete left siblings.

    Tree structure:
        BOSS
        ├── Worker1 (incomplete)
        └── Worker2 (should be blocked)
    """
    boss_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [worker1_id, worker2_id])
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, boss_id, sibling_index=0, completed=False)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, boss_id, sibling_index=1, completed=False)
        return []

    mock_event_store.get_events.side_effect = get_events

    await query_service._build_hierarchy_data(boss_id)

    # Worker1 has no left siblings, should not be blocked
    assert await query_service._has_incomplete_left_siblings(worker1_id) is False

    # Worker2 has incomplete left sibling (Worker1), should be blocked
    assert await query_service._has_incomplete_left_siblings(worker2_id) is True


@pytest.mark.asyncio
async def test_left_sibling_unblocked_when_complete(query_service, mock_event_store):
    """Test that a worker is unblocked when left siblings complete.

    Tree structure:
        BOSS
        ├── Worker1 (completed)
        └── Worker2 (should NOT be blocked)
    """
    boss_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [worker1_id, worker2_id])
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, boss_id, sibling_index=0, completed=True)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, boss_id, sibling_index=1, completed=False)
        return []

    mock_event_store.get_events.side_effect = get_events

    await query_service._build_hierarchy_data(boss_id)

    # Worker1 is complete, Worker2 should not be blocked
    assert await query_service._has_incomplete_left_siblings(worker2_id) is False


@pytest.mark.asyncio
async def test_left_sibling_blocking_nested(query_service, mock_event_store):
    """Test blocking with nested subtrees.

    Tree structure:
        BOSS
        ├── Manager1 (incomplete subtree)
        │   └── Worker1 (incomplete)
        └── Manager2 (should not have workers execute yet)
            └── Worker2

    Worker2 should be blocked because Manager1's subtree is incomplete.
    """
    boss_id = uuid4()
    manager1_id = uuid4()
    manager2_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [manager1_id, manager2_id])
        elif agent_id == manager1_id:
            return _create_manager_events(manager1_id, boss_id, [worker1_id], sibling_index=0)
        elif agent_id == manager2_id:
            return _create_manager_events(manager2_id, boss_id, [worker2_id], sibling_index=1)
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, manager1_id, sibling_index=0, completed=False)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, manager2_id, sibling_index=0, completed=False)
        return []

    mock_event_store.get_events.side_effect = get_events

    await query_service._build_hierarchy_data(boss_id)

    # Worker1 is leftmost, no blocking
    assert await query_service._has_incomplete_left_siblings(worker1_id) is False

    # Worker2's parent (Manager2) has left sibling (Manager1) with incomplete subtree
    assert await query_service._has_incomplete_left_siblings(worker2_id) is True


@pytest.mark.asyncio
async def test_get_active_agent_ids_sequential_workers(query_service, mock_event_store):
    """Test that sequential_workers=True returns only the leftmost eligible worker.

    Tree structure:
        BOSS
        ├── Worker1 (incomplete, should be returned)
        └── Worker2 (incomplete, should NOT be returned)
    """
    boss_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            # BOSS is completed (waiting is not terminal, let's mark as complete)
            events = _create_boss_events(boss_id, [worker1_id, worker2_id])
            events.append(WorkCompleted(aggregate_id=boss_id, sequence_number=10, result="Done"))
            return events
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, boss_id, sibling_index=0, completed=False)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, boss_id, sibling_index=1, completed=False)
        return []

    mock_event_store.get_events.side_effect = get_events

    active_ids = await query_service.get_active_agent_ids(boss_id, sequential_workers=True)

    # Only Worker1 should be returned (leftmost eligible)
    assert worker1_id in active_ids
    assert worker2_id not in active_ids


@pytest.mark.asyncio
async def test_get_active_agent_ids_allows_all_non_workers(query_service, mock_event_store):
    """Test that all non-terminal non-workers are returned regardless of position.

    Tree structure:
        BOSS (analyzing)
        ├── Manager1 (analyzing)
        └── Manager2 (analyzing)

    All managers should be returned since they don't modify workspace.
    """
    boss_id = uuid4()
    manager1_id = uuid4()
    manager2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [manager1_id, manager2_id])
        elif agent_id == manager1_id:
            return _create_manager_events(manager1_id, boss_id, sibling_index=0)
        elif agent_id == manager2_id:
            return _create_manager_events(manager2_id, boss_id, sibling_index=1)
        return []

    mock_event_store.get_events.side_effect = get_events

    active_ids = await query_service.get_active_agent_ids(boss_id, sequential_workers=True)

    # All non-terminal agents should be returned (no workers in this tree)
    assert boss_id in active_ids
    assert manager1_id in active_ids
    assert manager2_id in active_ids


@pytest.mark.asyncio
async def test_is_subtree_complete(query_service, mock_event_store):
    """Test subtree completeness check."""
    boss_id = uuid4()
    worker1_id = uuid4()
    worker2_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            events = _create_boss_events(boss_id, [worker1_id, worker2_id])
            events.append(WorkCompleted(aggregate_id=boss_id, sequence_number=10, result="Done"))
            return events
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, boss_id, sibling_index=0, completed=True)
        elif agent_id == worker2_id:
            return _create_worker_events(worker2_id, boss_id, sibling_index=1, completed=True)
        return []

    mock_event_store.get_events.side_effect = get_events

    await query_service._build_hierarchy_data(boss_id)

    # All agents are complete, subtree is complete
    assert await query_service._is_subtree_complete(boss_id) is True
    assert await query_service._is_subtree_complete(worker1_id) is True
    assert await query_service._is_subtree_complete(worker2_id) is True


@pytest.mark.asyncio
async def test_is_subtree_incomplete_with_pending_child(query_service, mock_event_store):
    """Test subtree is incomplete when any child is not terminal."""
    boss_id = uuid4()
    worker1_id = uuid4()

    def get_events(agent_id):
        if agent_id == boss_id:
            return _create_boss_events(boss_id, [worker1_id])
        elif agent_id == worker1_id:
            return _create_worker_events(worker1_id, boss_id, completed=False)
        return []

    mock_event_store.get_events.side_effect = get_events

    await query_service._build_hierarchy_data(boss_id)

    # Worker is not complete, so boss subtree is incomplete
    assert await query_service._is_subtree_complete(boss_id) is False
    assert await query_service._is_subtree_complete(worker1_id) is False
