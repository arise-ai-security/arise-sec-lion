"""DAG-aware task scheduler using Kahn's algorithm.

Schedules sibling agents based on depends_on edges. When no depends_on
edges exist, all agents are immediately ready (equivalent to parallel
execution, with left-to-right ordering handled by the query service).
"""

from dataclasses import dataclass, field


@dataclass
class TaskScheduler:
    """Schedules tasks in topological order based on dependency edges.

    Each task is identified by its sibling_index. Dependencies are expressed
    as lists of sibling indices that must complete before a task can run.

    Usage:
        scheduler = TaskScheduler.from_dependencies({
            0: [],        # no deps, ready immediately
            1: [0],       # depends on 0
            2: [0],       # depends on 0
            3: [1, 2],    # depends on both 1 and 2
        })

        ready = scheduler.get_ready()  # [0]
        scheduler.mark_completed(0)
        ready = scheduler.get_ready()  # [1, 2]
        scheduler.mark_completed(1)
        scheduler.mark_completed(2)
        ready = scheduler.get_ready()  # [3]
    """

    # {sibling_index: set of indices it depends on}
    _pending_deps: dict[int, set[int]] = field(default_factory=dict)
    # Indices that have been completed
    _completed: set[int] = field(default_factory=set)
    # Indices that have been failed or invalidated
    _failed: set[int] = field(default_factory=set)

    @classmethod
    def from_dependencies(cls, deps: dict[int, list[int]]) -> "TaskScheduler":
        scheduler = cls()
        scheduler._pending_deps = {idx: set(d) for idx, d in deps.items()}
        return scheduler

    def get_ready(self) -> list[int]:
        """Return indices whose dependencies are all satisfied.

        A task is ready when:
        - It hasn't been completed or failed
        - All its dependencies are in the completed set

        Returns indices sorted for deterministic ordering.
        """
        ready = []
        for idx, deps in self._pending_deps.items():
            if idx in self._completed or idx in self._failed:
                continue
            # All deps must be completed (not just removed)
            if deps.issubset(self._completed):
                ready.append(idx)
        ready.sort()
        return ready

    def mark_completed(self, index: int) -> None:
        self._completed.add(index)

    def mark_failed(self, index: int) -> None:
        self._failed.add(index)

    def invalidate_dependents(self, failed_index: int) -> list[int]:
        """Invalidate all tasks that transitively depend on a failed task.

        Returns list of newly invalidated indices.
        """
        invalidated = []
        queue = [failed_index]
        visited = set()

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            # Find all tasks that depend on current
            for idx, deps in self._pending_deps.items():
                if current in deps and idx not in self._failed:
                    self._failed.add(idx)
                    invalidated.append(idx)
                    queue.append(idx)

        return invalidated

    def is_complete(self) -> bool:
        """Check if all tasks are either completed or failed."""
        for idx in self._pending_deps:
            if idx not in self._completed and idx not in self._failed:
                return False
        return True

    def has_cycle(self) -> bool:
        """Detect cycles in the dependency graph."""
        # Kahn's algorithm: if we can't topologically sort all nodes, there's a cycle
        in_degree = {idx: len(deps) for idx, deps in self._pending_deps.items()}
        queue = [idx for idx, deg in in_degree.items() if deg == 0]
        count = 0

        while queue:
            node = queue.pop(0)
            count += 1
            for idx, deps in self._pending_deps.items():
                if node in deps:
                    in_degree[idx] -= 1
                    if in_degree[idx] == 0:
                        queue.append(idx)

        return count != len(self._pending_deps)
