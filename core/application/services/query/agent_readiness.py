"""DAG-aware readiness scheduling: which agents may run on the next loop tick.

Extracted from AgentQueryService: deciding worker eligibility (sibling DAG
edges, ancestor gating, left-to-right subtree ordering) is scheduling policy,
not a plain read — it changes for scheduling reasons, so it lives apart from
the CQRS read methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.application.services.query.read_models import AgentSummaryReadModel


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.services.lifecycle.agent_repository import AgentRepository


class AgentReadinessService:
    """Compute the set of agents eligible to take a step, in scheduling order."""

    def __init__(self, repository: AgentRepository) -> None:
        self._repository = repository

    async def get_active_agent_ids(  # noqa: PLR0912
        self,
        root_id: UUID | None = None,
        sequential_workers: bool = False,
    ) -> list[UUID]:
        """Return runnable IDs, including terminals with pending post-step work.

        Args:
            root_id: If provided, only return agents in this hierarchy.
                     Filters to agents where parent chain leads to root_id.
            sequential_workers: If True, only return one WORKER at a time in
                     left-to-right order. Non-workers (BOSS, MANAGER, PENDING)
                     are still returned in parallel for decomposition.

        Uses hierarchy-specific query when root_id is provided to avoid
        fetching events for unrelated agents.
        Uses lightweight read model instead of full aggregate reconstruction.
        """
        # Use hierarchy-specific query when root_id is provided
        if root_id is not None:
            all_events = await self._repository.get_hierarchy_events_grouped(root_id)
        else:
            all_events = await self._repository.get_all_events_grouped()

        summaries: dict[UUID, AgentSummaryReadModel] = {}
        for agent_id, events in all_events.items():
            summary = AgentSummaryReadModel.from_events(events)
            if summary is not None:
                summaries[agent_id] = summary

        # Build children map once for efficient tree operations
        children_map = self._build_children_map(summaries)

        # Pending terminal post-steps must run before normal dispatch. They bypass
        # DAG gates because retry scheduling and parent notification are recovery work.
        pending_post_steps: list[UUID] = []
        non_workers: list[UUID] = []
        workers: list[tuple[UUID, AgentSummaryReadModel]] = []

        for agent_id, summary in summaries.items():
            if summary.post_step_pending:
                pending_post_steps.append(agent_id)
                continue
            if summary.is_terminal:
                continue
            if not self._sibling_deps_satisfied(summary, summaries, children_map):
                continue
            if not self._ancestors_deps_satisfied(summary, summaries, children_map):
                continue
            if summary.role == "worker":
                # Only include worker if parent has finished spawning children
                # Parent must be in WAITING (spawned all children) or terminal state
                parent_id = summary.parent_id
                if parent_id and parent_id in summaries:
                    parent_summary = summaries[parent_id]
                    if parent_summary.status not in ("waiting", "completed", "failed"):
                        continue  # Skip worker - parent still spawning children
                workers.append((agent_id, summary))
            else:
                non_workers.append(agent_id)

        if not sequential_workers:
            return [*pending_post_steps, *non_workers, *(agent_id for agent_id, _ in workers)]

        if not workers:
            return [*pending_post_steps, *non_workers]

        # DAG-aware scheduling: check if workers' dependencies are satisfied
        # Group workers by parent for sibling-level dependency resolution
        eligible_workers: list[tuple[UUID, tuple[int, ...]]] = []

        has_dag_edges = any(s.depends_on for s in summaries.values())

        if has_dag_edges:
            # DAG mode: use depends_on edges to determine readiness
            eligible_workers = self._get_dag_ready_workers(workers, summaries, children_map)
        else:
            # Legacy mode: left-to-right ordering via subtree completion
            subtree_complete_cache: dict[UUID, bool] = {}
            self._compute_all_subtree_completions(summaries, children_map, subtree_complete_cache)

            for agent_id, _ in workers:
                if not self._has_incomplete_left_siblings_cached(
                    agent_id, summaries, children_map, subtree_complete_cache
                ):
                    path = self._compute_hierarchical_path_optimized(agent_id, summaries)
                    eligible_workers.append((agent_id, path))

        if not eligible_workers:
            return [*pending_post_steps, *non_workers]

        # Sort by path and return only the leftmost eligible worker
        eligible_workers.sort(key=lambda x: x[1])
        return [*pending_post_steps, *non_workers, eligible_workers[0][0]]

    def _get_dag_ready_workers(
        self,
        workers: list[tuple[UUID, AgentSummaryReadModel]],
        summaries: dict[UUID, AgentSummaryReadModel],
        children_map: dict[UUID | None, list[UUID]],
    ) -> list[tuple[UUID, tuple[int, ...]]]:
        """Get workers whose DAG dependencies are satisfied.

        A worker is ready when:
        1. All sibling_indices in its depends_on are terminal, AND
        2. All ancestor managers have their own sibling dependencies satisfied.

        Without check (2), a grandchild task under a blocked nested manager
        would execute before its parent's dependencies complete (e.g. sibling
        managers not yet finished).
        """
        eligible = []

        for agent_id, summary in workers:
            path = self._compute_hierarchical_path_optimized(agent_id, summaries)
            eligible.append((agent_id, path))

        return eligible

    @staticmethod
    def _sibling_deps_satisfied(
        summary: AgentSummaryReadModel,
        summaries: dict[UUID, AgentSummaryReadModel],
        children_map: dict[UUID | None, list[UUID]],
    ) -> bool:
        """Check if an agent's own sibling dependencies are all terminal."""
        if not summary.depends_on:
            return True
        parent_id = summary.parent_id
        if parent_id is None:
            return True
        siblings = children_map.get(parent_id, [])
        sibling_status: dict[int, str] = {}
        for sib_id in siblings:
            if sib_id in summaries:
                sib = summaries[sib_id]
                sibling_status[sib.sibling_index] = sib.status
        effective_deps = [d for d in summary.depends_on if d != summary.sibling_index]
        return all(
            AgentReadinessService._dependency_satisfied(
                sibling_status.get(dep_idx),
                summary.dependency_failure_policy,
            )
            for dep_idx in effective_deps
        )

    @staticmethod
    def _dependency_satisfied(status: str | None, failure_policy: str) -> bool:
        if status == "completed":
            return True
        return status == "failed" and failure_policy == "continue"

    @staticmethod
    def _ancestors_deps_satisfied(
        summary: AgentSummaryReadModel,
        summaries: dict[UUID, AgentSummaryReadModel],
        children_map: dict[UUID | None, list[UUID]],
    ) -> bool:
        """Walk up the parent chain and verify each ancestor's sibling deps are met.

        If any ancestor has unsatisfied sibling dependencies, this worker
        cannot run yet — its parent's phase hasn't been unblocked.
        """
        current_id = summary.parent_id
        while current_id is not None and current_id in summaries:
            ancestor = summaries[current_id]
            if ancestor.depends_on:
                ancestor_parent = ancestor.parent_id
                if ancestor_parent is not None:
                    siblings = children_map.get(ancestor_parent, [])
                    sibling_status: dict[int, str] = {}
                    for sib_id in siblings:
                        if sib_id in summaries:
                            sib = summaries[sib_id]
                            sibling_status[sib.sibling_index] = sib.status
                    effective_deps = [d for d in ancestor.depends_on if d != ancestor.sibling_index]
                    if not all(
                        AgentReadinessService._dependency_satisfied(
                            sibling_status.get(dep_idx),
                            ancestor.dependency_failure_policy,
                        )
                        for dep_idx in effective_deps
                    ):
                        return False
            current_id = ancestor.parent_id
        return True

    def _compute_hierarchical_path(
        self,
        agent_id: UUID,
        summaries: dict[UUID, AgentSummaryReadModel],
    ) -> tuple[int, ...]:
        """Compute hierarchical path for an agent.

        Returns a tuple of sibling indices from root to the agent.
        E.g., (0,) for root, (0, 1) for second child of root.
        This allows lexicographic sorting for left-to-right order.
        """
        path: list[int] = []
        current_id: UUID | None = agent_id

        while current_id is not None and current_id in summaries:
            summary = summaries[current_id]
            path.append(summary.sibling_index)
            current_id = summary.parent_id

        # Reverse to get root-to-leaf order
        return tuple(reversed(path))

    def _build_children_map(
        self,
        summaries: dict[UUID, AgentSummaryReadModel],
    ) -> dict[UUID | None, list[UUID]]:
        children_map: dict[UUID | None, list[UUID]] = {}
        for agent_id, summary in summaries.items():
            parent = summary.parent_id
            if parent not in children_map:
                children_map[parent] = []
            children_map[parent].append(agent_id)
        return children_map

    def _is_subtree_complete(
        self,
        root_id: UUID,
        summaries: dict[UUID, AgentSummaryReadModel],
        children_map: dict[UUID | None, list[UUID]],
    ) -> bool:
        """Check if entire subtree rooted at root_id is complete.

        A subtree is complete when ALL nodes (root and all descendants)
        are in terminal state (COMPLETED or FAILED).
        """
        if root_id not in summaries:
            return True

        summary = summaries[root_id]
        if not summary.is_terminal:
            return False

        # Recursively check all children
        for child_id in children_map.get(root_id, []):
            if not self._is_subtree_complete(child_id, summaries, children_map):
                return False

        return True

    def _compute_all_subtree_completions(
        self,
        summaries: dict[UUID, AgentSummaryReadModel],
        children_map: dict[UUID | None, list[UUID]],
        cache: dict[UUID, bool],
    ) -> None:
        """Pre-compute subtree completion for all agents in O(n) time.

        Uses bottom-up traversal: a node's subtree is complete iff the node
        is terminal AND all children's subtrees are complete.

        This eliminates O(n²) recursive checking by visiting each node once.
        """

        def compute_completion(agent_id: UUID) -> bool:
            if agent_id in cache:
                return cache[agent_id]

            summary = summaries.get(agent_id)
            if summary is None:
                cache[agent_id] = True
                return True

            # Must be terminal to be complete
            if not summary.is_terminal:
                cache[agent_id] = False
                return False

            # Check all children recursively (will use cache for already computed)
            for child_id in children_map.get(agent_id, []):
                if not compute_completion(child_id):
                    cache[agent_id] = False
                    return False

            cache[agent_id] = True
            return True

        for agent_id in summaries:
            if agent_id not in cache:
                compute_completion(agent_id)

    def _has_incomplete_left_siblings_cached(
        self,
        agent_id: UUID,
        summaries: dict[UUID, AgentSummaryReadModel],
        children_map: dict[UUID | None, list[UUID]],
        subtree_complete_cache: dict[UUID, bool],
    ) -> bool:
        """Check for incomplete left siblings using pre-computed cache.

        O(depth) instead of O(n) per call since subtree completion is cached.
        """
        current_id: UUID | None = agent_id

        while current_id is not None and current_id in summaries:
            summary = summaries[current_id]
            parent_id = summary.parent_id

            if parent_id is not None:
                current_sibling_index = summary.sibling_index
                for sibling_id in children_map.get(parent_id, []):
                    sibling = summaries[sibling_id]
                    # Use cached result instead of recursive computation.
                    if (
                        sibling.sibling_index < current_sibling_index
                        and not subtree_complete_cache.get(sibling_id, True)
                    ):
                        return True

            current_id = parent_id

        return False

    def _compute_hierarchical_path_optimized(
        self,
        agent_id: UUID,
        summaries: dict[UUID, AgentSummaryReadModel],
    ) -> tuple[int, ...]:
        """Compute hierarchical path without list reversal.

        Builds path by collecting indices then creating tuple in reverse order.
        Avoids: list allocation, append operations, and reversal.
        """
        # Count depth first to pre-allocate
        depth = 0
        current_id: UUID | None = agent_id
        while current_id is not None and current_id in summaries:
            depth += 1
            current_id = summaries[current_id].parent_id

        result = [0] * depth
        current_id = agent_id
        idx = depth - 1

        while current_id is not None and current_id in summaries:
            result[idx] = summaries[current_id].sibling_index
            current_id = summaries[current_id].parent_id
            idx -= 1

        return tuple(result)
