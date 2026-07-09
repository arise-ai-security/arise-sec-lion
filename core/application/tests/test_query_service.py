"""Test cases for AgentQueryService sequential worker ordering."""

from uuid import UUID, uuid4

import pytest

from core.application.services import AgentSummaryReadModel


class CountOnlyEventStore:
    """Fake event store exposing only the count API needed by this test."""

    def __init__(self, counts: dict[UUID, int]) -> None:
        self.counts = counts
        self.requested_ids: list[UUID] = []

    async def count_events(self, aggregate_ids: list[UUID]) -> dict[UUID, int]:
        self.requested_ids = aggregate_ids
        return {
            aggregate_id: self.counts.get(aggregate_id, 0)
            for aggregate_id in aggregate_ids
        }


class TestEventCounts:
    """Tests for event count query delegation."""

    async def test_get_event_counts_uses_repository_count_query(self) -> None:
        """Event count query does not require full event fetch support."""
        from core.application.services import AgentQueryService, AgentRepository

        # Given: A real repository backed by a store with count_events only
        first_id = uuid4()
        second_id = uuid4()
        missing_id = uuid4()
        event_store = CountOnlyEventStore({first_id: 3, second_id: 1})
        repository = AgentRepository(event_store=event_store)
        service = AgentQueryService(repository)

        # When: Fetching event counts through the query service
        counts = await service.get_event_counts([first_id, second_id, missing_id])

        # Then: The query delegates to count_events without fetching full events
        assert counts == {first_id: 3, second_id: 1, missing_id: 0}
        assert event_store.requested_ids == [first_id, second_id, missing_id]

    async def test_get_event_counts_returns_empty_without_store_call(self) -> None:
        """Empty input avoids unnecessary repository calls."""
        from core.application.services import AgentQueryService, AgentRepository

        # Given: A real repository backed by a count-capable fake
        event_store = CountOnlyEventStore({})
        repository = AgentRepository(event_store=event_store)
        service = AgentQueryService(repository)

        # When: Fetching counts for no agents
        counts = await service.get_event_counts([])

        # Then: The service returns immediately
        assert counts == {}
        assert event_store.requested_ids == []


class TestAgentSummaryReadModel:
    """Tests for AgentSummaryReadModel sibling_index handling."""

    def test_sibling_index_extracted_from_agent_created(self) -> None:
        """Test that sibling_index is correctly extracted from AgentCreated event."""
        from core.domain.events.events import AgentCreated

        # Given: An AgentCreated event with sibling_index=2
        agent_id = uuid4()
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role="worker",
            parent_id=uuid4(),
            config={},
            sibling_index=2,
        )

        # When: Building read model from events
        summary = AgentSummaryReadModel.from_events([event])

        # Then: sibling_index is correctly extracted
        assert summary is not None
        assert summary.sibling_index == 2

    def test_sibling_index_defaults_to_zero(self) -> None:
        """Test that sibling_index defaults to 0 for root agents."""
        from core.domain.events.events import AgentCreated

        # Given: An AgentCreated event without explicit sibling_index
        agent_id = uuid4()
        event = AgentCreated(
            aggregate_id=agent_id,
            sequence_number=1,
            role="boss",
            parent_id=None,
            config={},
        )

        # When: Building read model from events
        summary = AgentSummaryReadModel.from_events([event])

        # Then: sibling_index defaults to 0
        assert summary is not None
        assert summary.sibling_index == 0


class TestHierarchicalPathComputation:
    """Tests for _compute_hierarchical_path method."""

    def _build_summaries(
        self,
        tree_structure: list[tuple[UUID, UUID | None, int]],
    ) -> dict[UUID, AgentSummaryReadModel]:
        """Build test summaries from tree structure.

        Args:
            tree_structure: List of (agent_id, parent_id, sibling_index) tuples.
        """
        return {
            agent_id: AgentSummaryReadModel(
                agent_id=agent_id,
                role="worker",
                status="analyzing",
                parent_id=parent_id,
                task_summary="test",
                is_terminal=False,
                sibling_index=sibling_index,
                depends_on=(),
            )
            for agent_id, parent_id, sibling_index in tree_structure
        }

    def test_root_agent_path(self) -> None:
        """Test that root agent has path (0,)."""
        from core.application.services import AgentReadinessService

        # Given: A root agent with sibling_index=0
        root_id = uuid4()
        summaries = self._build_summaries([(root_id, None, 0)])

        # When: Computing path
        service = AgentReadinessService.__new__(AgentReadinessService)
        path = service._compute_hierarchical_path(root_id, summaries)

        # Then: Path is (0,)
        assert path == (0,)

    def test_child_agent_path(self) -> None:
        """Test that child agent has path (parent_sibling, child_sibling)."""
        from core.application.services import AgentReadinessService

        # Given: A tree with root and second child (sibling_index=1)
        root_id = uuid4()
        child_id = uuid4()
        summaries = self._build_summaries([
            (root_id, None, 0),
            (child_id, root_id, 1),  # Second child
        ])

        # When: Computing path for child
        service = AgentReadinessService.__new__(AgentReadinessService)
        path = service._compute_hierarchical_path(child_id, summaries)

        # Then: Path is (0, 1) - root's index 0, child's index 1
        assert path == (0, 1)

    def test_grandchild_agent_path(self) -> None:
        """Test that grandchild has correct 3-level path."""
        from core.application.services import AgentReadinessService

        # Given: A 3-level tree
        root_id = uuid4()
        child_id = uuid4()
        grandchild_id = uuid4()
        summaries = self._build_summaries([
            (root_id, None, 0),
            (child_id, root_id, 2),       # Third child of root
            (grandchild_id, child_id, 1),  # Second child of child
        ])

        # When: Computing path for grandchild
        service = AgentReadinessService.__new__(AgentReadinessService)
        path = service._compute_hierarchical_path(grandchild_id, summaries)

        # Then: Path is (0, 2, 1)
        assert path == (0, 2, 1)

    def test_path_ordering_is_left_to_right(self) -> None:
        """Test that lexicographic sorting gives left-to-right order."""
        from core.application.services import AgentReadinessService

        # Given: A tree like:
        #        BOSS (0)
        #       /    \
        #     M1(0)  M2(1)
        #    /  \    /  \
        #   W1  W2  W3  W4
        #   (0) (1) (0) (1)
        root_id = uuid4()
        m1_id, m2_id = uuid4(), uuid4()
        w1_id, w2_id, w3_id, w4_id = uuid4(), uuid4(), uuid4(), uuid4()

        summaries = self._build_summaries([
            (root_id, None, 0),
            (m1_id, root_id, 0),
            (m2_id, root_id, 1),
            (w1_id, m1_id, 0),
            (w2_id, m1_id, 1),
            (w3_id, m2_id, 0),
            (w4_id, m2_id, 1),
        ])

        # When: Computing paths for all workers
        service = AgentReadinessService.__new__(AgentReadinessService)
        worker_paths = [
            (w_id, service._compute_hierarchical_path(w_id, summaries))
            for w_id in [w1_id, w2_id, w3_id, w4_id]
        ]

        # Then: Sorting by path gives left-to-right order
        worker_paths.sort(key=lambda x: x[1])
        sorted_ids = [w_id for w_id, _ in worker_paths]

        assert sorted_ids == [w1_id, w2_id, w3_id, w4_id]


class TestSequentialWorkerOrdering:
    """Tests for get_active_agent_ids with sequential_workers=True."""

    @pytest.fixture
    def mock_repository(self):
        """Create a mock repository that returns predefined events."""
        from unittest.mock import AsyncMock, MagicMock

        from core.domain.events.events import AgentCreated, TaskAssigned

        def create_events_for_agent(
            agent_id: UUID,
            role: str,
            parent_id: UUID | None,
            sibling_index: int,
            status: str = "analyzing",
        ) -> list:
            from core.domain.events.events import StatusChanged

            events = [
                AgentCreated(
                    aggregate_id=agent_id,
                    sequence_number=1,
                    role=role,
                    parent_id=parent_id,
                    config={},
                    sibling_index=sibling_index,
                ),
                TaskAssigned(
                    aggregate_id=agent_id,
                    sequence_number=2,
                    task_description="test task",
                ),
            ]
            # Add status transition if not default "analyzing"
            if status != "analyzing":
                events.append(
                    StatusChanged(
                        aggregate_id=agent_id,
                        sequence_number=3,
                        old_status="analyzing",
                        new_status=status,
                    )
                )
            return events

        # Create the repository mock
        repository = MagicMock()
        repository.get_all_events_grouped = AsyncMock()

        return repository, create_events_for_agent

    @pytest.mark.asyncio
    async def test_sequential_workers_returns_only_leftmost_worker(
        self, mock_repository
    ) -> None:
        """Test that sequential_workers=True returns only the leftmost active worker."""
        from core.application.services import AgentReadinessService

        repository, create_events = mock_repository

        # Given: A tree with two workers
        root_id = uuid4()
        w1_id, w2_id = uuid4(), uuid4()

        repository.get_all_events_grouped.return_value = {
            root_id: create_events(root_id, "boss", None, 0, "completed"),
            w1_id: create_events(w1_id, "worker", root_id, 0),
            w2_id: create_events(w2_id, "worker", root_id, 1),
        }

        # Mark boss as completed
        from core.domain.events.events import WorkCompleted

        repository.get_all_events_grouped.return_value[root_id].append(
            WorkCompleted(aggregate_id=root_id, sequence_number=3, result="done")
        )

        # When: Getting active agents with sequential_workers=True
        service = AgentReadinessService(repository)
        active = await service.get_active_agent_ids(sequential_workers=True)

        # Then: Only w1 (leftmost) should be returned
        assert w1_id in active
        assert w2_id not in active

    @pytest.mark.asyncio
    async def test_non_workers_returned_in_parallel(self, mock_repository) -> None:
        """Test that non-workers are still returned even with sequential_workers."""
        from core.application.services import AgentReadinessService

        repository, create_events = mock_repository

        # Given: A manager (completed) and workers as siblings under root
        root_id = uuid4()
        m1_id = uuid4()
        w1_id, w2_id = uuid4(), uuid4()

        repository.get_all_events_grouped.return_value = {
            root_id: create_events(root_id, "boss", None, 0, "waiting"),
            m1_id: create_events(m1_id, "manager", root_id, 0, "completed"),
            w1_id: create_events(w1_id, "worker", root_id, 1),
            w2_id: create_events(w2_id, "worker", root_id, 2),
        }

        # Mark manager as completed so workers can proceed
        from core.domain.events.events import WorkCompleted

        repository.get_all_events_grouped.return_value[m1_id].append(
            WorkCompleted(aggregate_id=m1_id, sequence_number=3, result="done")
        )

        # When: Getting active agents with sequential_workers=True
        service = AgentReadinessService(repository)
        active = await service.get_active_agent_ids(sequential_workers=True)

        # Then: Root (non-worker) is returned plus only leftmost worker
        # Manager is terminal so not in active list
        assert root_id in active
        assert m1_id not in active  # Manager completed
        assert w1_id in active
        assert w2_id not in active

    @pytest.mark.asyncio
    async def test_workers_blocked_by_incomplete_left_sibling_subtree(
        self, mock_repository
    ) -> None:
        """Test that workers wait for incomplete left sibling subtrees.

        Scenario: Left subtree has a manager still decomposing (no workers yet),
        right subtree has workers ready. Workers in right subtree should NOT execute
        until the left subtree is complete.
        """
        from core.application.services import AgentReadinessService

        repository, create_events = mock_repository

        # Given: Two sibling managers, left still decomposing, right has workers
        root_id = uuid4()
        m_left_id = uuid4()  # Left manager, still analyzing
        m_right_id = uuid4()  # Right manager, completed
        w_right_id = uuid4()  # Worker under right manager

        repository.get_all_events_grouped.return_value = {
            root_id: create_events(root_id, "boss", None, 0, "waiting"),
            m_left_id: create_events(m_left_id, "manager", root_id, 0),  # Not terminal
            m_right_id: create_events(m_right_id, "manager", root_id, 1, "completed"),
            w_right_id: create_events(w_right_id, "worker", m_right_id, 0),
        }

        # Mark right manager as completed
        from core.domain.events.events import WorkCompleted

        repository.get_all_events_grouped.return_value[m_right_id].append(
            WorkCompleted(aggregate_id=m_right_id, sequence_number=3, result="done")
        )

        # When: Getting active agents with sequential_workers=True
        service = AgentReadinessService(repository)
        active = await service.get_active_agent_ids(sequential_workers=True)

        # Then: Worker is blocked because left sibling subtree is incomplete
        assert root_id in active  # Non-worker, still active
        assert m_left_id in active  # Non-worker, still active (decomposing)
        assert m_right_id not in active  # Terminal
        assert w_right_id not in active  # Blocked by incomplete left sibling

    @pytest.mark.asyncio
    async def test_workers_unblocked_when_left_sibling_complete(
        self, mock_repository
    ) -> None:
        """Test that workers can proceed once left sibling subtrees are complete."""
        from core.application.services import AgentReadinessService

        repository, create_events = mock_repository

        # Given: Two sibling managers, both completed, right has workers
        root_id = uuid4()
        m_left_id = uuid4()
        m_right_id = uuid4()
        w_right_id = uuid4()

        repository.get_all_events_grouped.return_value = {
            root_id: create_events(root_id, "boss", None, 0, "waiting"),
            m_left_id: create_events(m_left_id, "manager", root_id, 0, "completed"),
            m_right_id: create_events(m_right_id, "manager", root_id, 1, "completed"),
            w_right_id: create_events(w_right_id, "worker", m_right_id, 0),
        }

        # Mark both managers as completed
        from core.domain.events.events import WorkCompleted

        repository.get_all_events_grouped.return_value[m_left_id].append(
            WorkCompleted(aggregate_id=m_left_id, sequence_number=3, result="done")
        )
        repository.get_all_events_grouped.return_value[m_right_id].append(
            WorkCompleted(aggregate_id=m_right_id, sequence_number=3, result="done")
        )

        # When: Getting active agents with sequential_workers=True
        service = AgentReadinessService(repository)
        active = await service.get_active_agent_ids(sequential_workers=True)

        # Then: Worker can proceed (left sibling is complete)
        assert root_id in active
        assert m_left_id not in active  # Terminal
        assert m_right_id not in active  # Terminal
        assert w_right_id in active  # No longer blocked

    @pytest.mark.asyncio
    async def test_workers_blocked_by_incomplete_nested_subtree(
        self, mock_repository
    ) -> None:
        """Test that workers wait for nested left subtrees to complete.

        Scenario: Left manager is completed but has children still working.
        Right worker should NOT execute until entire left subtree is done.
        """
        from core.application.services import AgentReadinessService

        repository, create_events = mock_repository

        # Given: Left manager completed, but has a child manager still working
        root_id = uuid4()
        m_left_id = uuid4()  # Left manager - completed
        m_left_child_id = uuid4()  # Child of left manager - still working!
        w_right_id = uuid4()  # Right worker

        repository.get_all_events_grouped.return_value = {
            root_id: create_events(root_id, "boss", None, 0, "waiting"),
            m_left_id: create_events(m_left_id, "manager", root_id, 0, "completed"),
            m_left_child_id: create_events(
                m_left_child_id, "manager", m_left_id, 0
            ),  # Not terminal
            w_right_id: create_events(w_right_id, "worker", root_id, 1),
        }

        # Mark left manager as completed (but child is NOT completed)
        from core.domain.events.events import WorkCompleted

        repository.get_all_events_grouped.return_value[m_left_id].append(
            WorkCompleted(aggregate_id=m_left_id, sequence_number=3, result="done")
        )

        # When: Getting active agents with sequential_workers=True
        service = AgentReadinessService(repository)
        active = await service.get_active_agent_ids(sequential_workers=True)

        # Then: Worker is blocked because left subtree has incomplete child
        assert root_id in active
        assert m_left_id not in active  # Terminal (completed)
        assert m_left_child_id in active  # Non-worker, still working
        assert w_right_id not in active  # Blocked! Left subtree not fully complete

    @pytest.mark.asyncio
    async def test_sequential_false_returns_all_workers(self, mock_repository) -> None:
        """Test that sequential_workers=False returns all active workers."""
        from core.application.services import AgentReadinessService

        repository, create_events = mock_repository

        # Given: A tree with two workers
        root_id = uuid4()
        w1_id, w2_id = uuid4(), uuid4()

        repository.get_all_events_grouped.return_value = {
            root_id: create_events(root_id, "boss", None, 0, "completed"),
            w1_id: create_events(w1_id, "worker", root_id, 0),
            w2_id: create_events(w2_id, "worker", root_id, 1),
        }

        from core.domain.events.events import WorkCompleted

        repository.get_all_events_grouped.return_value[root_id].append(
            WorkCompleted(aggregate_id=root_id, sequence_number=3, result="done")
        )

        # When: Getting active agents with sequential_workers=False (default)
        service = AgentReadinessService(repository)
        active = await service.get_active_agent_ids(sequential_workers=False)

        # Then: Both workers are returned
        assert w1_id in active
        assert w2_id in active


class TestSiblingResultSummary:
    """Sibling-facing result summaries prefer the worker's conclusion."""

    def test_prefers_text_after_conclusion_marker(self) -> None:
        # Given: a judge-oriented command log followed by a conclusion
        from core.application.services.query.sibling_view import _sibling_result_summary
        from core.domain.values.node_message import WORKER_CONCLUSION_MARKER

        result = (
            "$ view (exit None)\nInvalid `path` parameter: /src/missing.c\n"
            f"{WORKER_CONCLUSION_MARKER}\nBuilt the PoC; repro.sh reproduces the crash."
        )

        # When
        summary = _sibling_result_summary(result)

        # Then: siblings see the conclusion, not the transcript noise
        assert summary == "Built the PoC; repro.sh reproduces the crash."
        assert "Invalid `path`" not in summary

    def test_falls_back_to_head_tail_without_marker(self) -> None:
        # Given: a result with no conclusion recorded
        from core.application.services.query.sibling_view import _sibling_result_summary

        result = "$ make (exit 0)\nok\n" * 200

        # When
        summary = _sibling_result_summary(result, limit=100)

        # Then: legacy head/tail truncation
        assert "chars omitted" in summary
        assert len(summary) < len(result)

    def test_empty_conclusion_falls_back_to_full_log(self) -> None:
        # Given: a marker with nothing after it
        from core.application.services.query.sibling_view import _sibling_result_summary
        from core.domain.values.node_message import WORKER_CONCLUSION_MARKER

        result = f"$ make (exit 0)\nok\n{WORKER_CONCLUSION_MARKER}\n   "

        # When
        summary = _sibling_result_summary(result)

        # Then
        assert "$ make (exit 0)" in summary
