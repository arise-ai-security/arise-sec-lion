"""Tests for DAG-aware TaskScheduler."""

from core.domain.services.task_scheduler import TaskScheduler


class TestTaskSchedulerBasic:
    """Basic scheduling with no dependencies."""

    def test_all_ready_when_no_deps(self) -> None:
        scheduler = TaskScheduler.from_dependencies({0: [], 1: [], 2: []})
        assert scheduler.get_ready() == [0, 1, 2]

    def test_empty_scheduler(self) -> None:
        scheduler = TaskScheduler.from_dependencies({})
        assert scheduler.get_ready() == []
        assert scheduler.is_complete()

    def test_single_task(self) -> None:
        scheduler = TaskScheduler.from_dependencies({0: []})
        assert scheduler.get_ready() == [0]
        scheduler.mark_completed(0)
        assert scheduler.get_ready() == []
        assert scheduler.is_complete()


class TestTaskSchedulerLinearChain:
    """Linear dependency chain: 0 → 1 → 2."""

    def test_linear_chain(self) -> None:
        scheduler = TaskScheduler.from_dependencies({
            0: [],
            1: [0],
            2: [1],
        })

        assert scheduler.get_ready() == [0]
        scheduler.mark_completed(0)
        assert scheduler.get_ready() == [1]
        scheduler.mark_completed(1)
        assert scheduler.get_ready() == [2]
        scheduler.mark_completed(2)
        assert scheduler.is_complete()


class TestTaskSchedulerDiamond:
    """Diamond pattern: 0 → {1, 2} → 3."""

    def test_diamond_dependency(self) -> None:
        scheduler = TaskScheduler.from_dependencies({
            0: [],
            1: [0],
            2: [0],
            3: [1, 2],
        })

        assert scheduler.get_ready() == [0]
        scheduler.mark_completed(0)
        assert scheduler.get_ready() == [1, 2]

        scheduler.mark_completed(1)
        assert scheduler.get_ready() == [2]  # 3 still waiting on 2

        scheduler.mark_completed(2)
        assert scheduler.get_ready() == [3]

    def test_diamond_partial_completion(self) -> None:
        scheduler = TaskScheduler.from_dependencies({
            0: [],
            1: [0],
            2: [0],
            3: [1, 2],
        })

        scheduler.mark_completed(0)
        scheduler.mark_completed(2)
        # 3 still blocked by 1
        assert 3 not in scheduler.get_ready()


class TestTaskSchedulerFailure:
    """Failure handling and dependent invalidation."""

    def test_failed_task_not_ready(self) -> None:
        scheduler = TaskScheduler.from_dependencies({0: [], 1: []})
        scheduler.mark_failed(0)
        assert scheduler.get_ready() == [1]

    def test_invalidate_dependents(self) -> None:
        scheduler = TaskScheduler.from_dependencies({
            0: [],
            1: [0],
            2: [1],
        })

        scheduler.mark_failed(0)
        invalidated = scheduler.invalidate_dependents(0)
        assert 1 in invalidated
        assert 2 in invalidated
        assert scheduler.is_complete()

    def test_invalidate_only_affected_branch(self) -> None:
        scheduler = TaskScheduler.from_dependencies({
            0: [],
            1: [],
            2: [0],
            3: [1],
        })

        scheduler.mark_failed(0)
        invalidated = scheduler.invalidate_dependents(0)
        assert 2 in invalidated
        assert 3 not in invalidated
        # Task 1 and 3 unaffected
        assert scheduler.get_ready() == [1]


class TestTaskSchedulerCycleDetection:
    """Cycle detection."""

    def test_no_cycle(self) -> None:
        scheduler = TaskScheduler.from_dependencies({0: [], 1: [0], 2: [1]})
        assert not scheduler.has_cycle()

    def test_detects_cycle(self) -> None:
        scheduler = TaskScheduler.from_dependencies({0: [1], 1: [0]})
        assert scheduler.has_cycle()

    def test_detects_self_cycle(self) -> None:
        scheduler = TaskScheduler.from_dependencies({0: [0]})
        assert scheduler.has_cycle()
