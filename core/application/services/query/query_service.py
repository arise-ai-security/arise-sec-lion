"""Agent query service for read operations (CQRS read side).

Handles statistics, results, and active agent queries.
"""



from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from core.application.dtos import AgentResultDTO, SystemStatisticsDTO
from core.domain.events.events import (
    AgentCreated,
    CodeGenerationStarted,
    ComplexityEvaluated,
    DecisionInfeasible,
    DomainEvent,
    RedecompositionTriggered,
    RetryScheduled,
    StatusChanged,
    TaskAssigned,
    VerificationFailed,
    WorkCompleted,
    WorkFailed,
)
from core.domain.values.node_message import Handoff, PeerStatus, SharedDecision


def _head_tail(text: str, limit: int) -> str:
    """Keep first 2/3 + last 1/3 of text, showing omission count."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n\n[...{omitted} chars omitted...]\n\n{text[-tail:]}"


if TYPE_CHECKING:
    from core.application.services.lifecycle.agent_repository import AgentRepository
    from core.ports.runtime_ports import SharedContextPort


@dataclass(frozen=True)
class AgentSummaryReadModel:
    """Lightweight read model for agent queries.

    Avoids full aggregate reconstruction by extracting only needed fields
    directly from events. Much faster than AgentSession.load_from_history().
    """

    agent_id: UUID
    role: str
    status: str
    parent_id: UUID | None
    task_summary: str
    is_terminal: bool
    sibling_index: int  # Position among siblings for ordering
    depends_on: tuple[int, ...]  # Sibling indices this agent depends on (DAG scheduling)

    @classmethod
    def from_events(cls, events: list[DomainEvent]) -> "AgentSummaryReadModel | None":
        """Build read model from events without full aggregate reconstruction.

        Only processes the events needed to extract summary fields:
        - AgentCreated: initial role, parent_id, sibling_index
        - TaskAssigned: task_description
        - ComplexityEvaluated: updated role (PENDING -> WORKER/MANAGER)
        - StatusChanged/CodeGenerationStarted/WorkCompleted/WorkFailed: current status

        Args:
            events: List of domain events for an aggregate.

        Returns:
            AgentSummaryReadModel or None if events are invalid.
        """
        if not events:
            return None

        first_event = events[0]
        if not isinstance(first_event, AgentCreated):
            return None

        # Extract from AgentCreated
        agent_id = first_event.aggregate_id
        role = first_event.role
        parent_id = first_event.parent_id
        sibling_index = first_event.sibling_index
        depends_on = tuple(first_event.depends_on)

        # Default values
        task_summary = ""
        status = "pending"  # Default after creation

        # Process events to extract status, task, and role changes
        for event in events:
            if isinstance(event, TaskAssigned):
                task_summary = event.task_description[:100]  # Truncate for summary
                status = "analyzing"  # TaskAssigned transitions to ANALYZING
            elif isinstance(event, ComplexityEvaluated):
                # CRITICAL: Update role when complexity evaluation determines WORKER/MANAGER
                # Without this, PENDING agents that become WORKER are not detected as workers
                # and bypass the sequential execution check!
                role = event.determined_role
            elif isinstance(event, StatusChanged):
                status = event.new_status
            elif isinstance(event, CodeGenerationStarted):
                status = "in_progress"
            elif isinstance(event, WorkCompleted):
                status = "completed"
            elif isinstance(event, (WorkFailed, VerificationFailed, DecisionInfeasible)):
                status = "failed"
            elif isinstance(event, RetryScheduled):
                # FAILED → ANALYZING: agent is retrying, no longer terminal
                status = "analyzing"
            elif isinstance(event, RedecompositionTriggered):
                # WAITING → ANALYZING: parent re-decomposes after child infeasible
                status = "analyzing"

        is_terminal = status in ("completed", "failed")

        return cls(
            agent_id=agent_id,
            role=role,
            status=status,
            parent_id=parent_id,
            task_summary=task_summary,
            is_terminal=is_terminal,
            sibling_index=sibling_index,
            depends_on=depends_on,
        )


class AgentQueryService:
    """Query service for agent read operations.

    Single Responsibility: Handle read-only queries about agents.
    CQRS pattern: Separates read operations from write operations.
    """

    def __init__(
        self,
        repository: "AgentRepository",
        shared_context_port: "SharedContextPort | None" = None,
    ) -> None:
        self._repository = repository
        self._shared_context_port = shared_context_port

    async def get_result(self, agent_id: UUID) -> AgentResultDTO:
        """Get agent result as DTO.

        Raises:
            AgentNotFoundError: If agent doesn't exist.
        """
        agent = await self._repository.load(agent_id)
        return AgentResultDTO(
            agent_id=str(agent.agent_id),
            status=agent.status.value,
            result=agent.result,
            task_description=agent.task_description or "",
            role=agent.role.value,
        )

    async def get_statistics(
        self,
        root_id: UUID | None = None,
    ) -> SystemStatisticsDTO:
        """Get statistics about agents.

        Args:
            root_id: If provided, only count agents in this hierarchy.
                     If None, counts all agents in the database.

        Uses hierarchy-specific query when root_id is provided for accurate
        per-run statistics. Uses lightweight read model instead of full
        aggregate reconstruction.
        """
        if root_id is not None:
            all_events = await self._repository.get_hierarchy_events_grouped(root_id)
        else:
            all_events = await self._repository.get_all_events_grouped()

        total = 0
        completed = 0
        failed = 0
        active = 0

        for events in all_events.values():
            summary = AgentSummaryReadModel.from_events(events)
            if summary is None:
                continue

            total += 1
            if summary.status == "completed":
                completed += 1
            elif summary.status == "failed":
                failed += 1
            else:
                active += 1

        return SystemStatisticsDTO(
            total_agents=total,
            completed=completed,
            failed=failed,
            active=active,
        )

    async def get_event_counts(
        self, agent_ids: list[UUID]
    ) -> dict[UUID, int]:
        """Return number of persisted events per agent.

        Used by the system loop's staleness watchdog to detect agents that
        keep getting re-scheduled by ``get_active_agent_ids`` but never emit
        any new events (boss-in-judge-loop, manager re-decomposition cycle,
        etc.) — a pattern the per-task wall-clock watchdog cannot catch
        because each individual ``run_agent_step`` call completes quickly.
        """
        if not agent_ids:
            return {}
        return await self._repository.get_event_counts(agent_ids)

    async def get_active_agent_ids(  # noqa: PLR0912
        self,
        root_id: UUID | None = None,
        sequential_workers: bool = False,
    ) -> list[UUID]:
        """Return agent IDs not in terminal state (COMPLETED/FAILED).

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

        # Get non-terminal agents (single pass, combined filtering)
        non_workers: list[UUID] = []
        workers: list[tuple[UUID, AgentSummaryReadModel]] = []

        for agent_id, summary in summaries.items():
            if summary.is_terminal:
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
            return non_workers + [agent_id for agent_id, _ in workers]

        if not workers:
            return non_workers

        # DAG-aware scheduling: check if workers' dependencies are satisfied
        # Group workers by parent for sibling-level dependency resolution
        eligible_workers: list[tuple[UUID, tuple[int, ...]]] = []

        has_dag_edges = any(s.depends_on for _, s in workers)

        if has_dag_edges:
            # DAG mode: use depends_on edges to determine readiness
            eligible_workers = self._get_dag_ready_workers(
                workers, summaries, children_map
            )
        else:
            # Legacy mode: left-to-right ordering via subtree completion
            subtree_complete_cache: dict[UUID, bool] = {}
            self._compute_all_subtree_completions(
                summaries, children_map, subtree_complete_cache
            )

            for agent_id, _ in workers:
                if not self._has_incomplete_left_siblings_cached(
                    agent_id, summaries, children_map, subtree_complete_cache
                ):
                    path = self._compute_hierarchical_path_optimized(
                        agent_id, summaries
                    )
                    eligible_workers.append((agent_id, path))

        if not eligible_workers:
            return non_workers

        # Sort by path and return only the leftmost eligible worker
        eligible_workers.sort(key=lambda x: x[1])
        return [*non_workers, eligible_workers[0][0]]

    def _get_dag_ready_workers(
        self,
        workers: list[tuple[UUID, "AgentSummaryReadModel"]],
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
            if not self._sibling_deps_satisfied(summary, summaries, children_map):
                continue
            if not self._ancestors_deps_satisfied(summary, summaries, children_map):
                continue
            path = self._compute_hierarchical_path_optimized(agent_id, summaries)
            eligible.append((agent_id, path))

        return eligible

    @staticmethod
    def _sibling_deps_satisfied(
        summary: "AgentSummaryReadModel",
        summaries: dict[UUID, "AgentSummaryReadModel"],
        children_map: dict[UUID | None, list[UUID]],
    ) -> bool:
        """Check if an agent's own sibling dependencies are all terminal."""
        if not summary.depends_on:
            return True
        parent_id = summary.parent_id
        if parent_id is None:
            return True
        siblings = children_map.get(parent_id, [])
        sibling_status: dict[int, bool] = {}
        for sib_id in siblings:
            if sib_id in summaries:
                sib = summaries[sib_id]
                sibling_status[sib.sibling_index] = sib.is_terminal
        effective_deps = [
            d for d in summary.depends_on if d != summary.sibling_index
        ]
        return all(
            sibling_status.get(dep_idx, False) for dep_idx in effective_deps
        )

    @staticmethod
    def _ancestors_deps_satisfied(
        summary: "AgentSummaryReadModel",
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
                    sibling_status: dict[int, bool] = {}
                    for sib_id in siblings:
                        if sib_id in summaries:
                            sib = summaries[sib_id]
                            sibling_status[sib.sibling_index] = sib.is_terminal
                    effective_deps = [
                        d for d in ancestor.depends_on
                        if d != ancestor.sibling_index
                    ]
                    if not all(
                        sibling_status.get(dep_idx, False)
                        for dep_idx in effective_deps
                    ):
                        return False
            current_id = ancestor.parent_id
        return True

    def _compute_hierarchical_path(
        self,
        agent_id: UUID,
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
        summaries: dict[UUID, "AgentSummaryReadModel"],
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
        summaries: dict[UUID, "AgentSummaryReadModel"],
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

    # -------------------------------------------------------------------------
    # Sibling View (implements SiblingViewPort)
    # -------------------------------------------------------------------------

    async def build_view(
        self,
        agent_id: UUID,
        parent_id: UUID | None,
        root_id: UUID,
    ) -> Handoff:
        parent_task = await self._get_parent_task(parent_id)
        current_sibling_index = await self._get_current_sibling_index(agent_id)
        sibling_statuses = await self._get_sibling_statuses(agent_id, parent_id)
        shared_decisions = await self._get_shared_decisions(root_id)

        return Handoff(
            parent_task=parent_task,
            current_sibling_index=current_sibling_index,
            siblings=tuple(sibling_statuses),
            shared_decisions=tuple(shared_decisions),
        )

    async def _get_parent_task(self, parent_id: UUID | None) -> str | None:
        if parent_id is None:
            return None
        parent = await self._repository.load_if_exists(parent_id)
        return parent.task_description if parent else None

    async def _get_current_sibling_index(self, agent_id: UUID) -> int | None:
        agent = await self._repository.load_if_exists(agent_id)
        if agent is None:
            return None
        return agent.sibling_index

    async def _get_sibling_statuses(
        self, agent_id: UUID, parent_id: UUID | None
    ) -> list[PeerStatus]:
        if parent_id is None:
            return []

        children_events = await self._repository.get_children_events_grouped(parent_id)

        siblings: list[PeerStatus] = []
        for agg_id, events in children_events.items():
            if agg_id == agent_id:
                continue

            summary = AgentSummaryReadModel.from_events(events)
            if summary is None:
                continue

            result_summary = None
            if summary.status == "completed":
                agent = await self._repository.load_if_exists(agg_id)
                if agent and agent.result:
                    result_summary = _head_tail(agent.result, 2000)

            siblings.append(
                PeerStatus(
                    agent_id=str(agg_id),
                    sibling_index=summary.sibling_index,
                    status=summary.status,
                    task_summary=summary.task_summary[:200],
                    result_summary=result_summary,
                )
            )

        siblings.sort(key=lambda s: s.sibling_index)
        return siblings

    async def _get_shared_decisions(self, root_id: UUID) -> list[SharedDecision]:
        if self._shared_context_port is None:
            return []

        context = await self._shared_context_port.get(root_id)
        if context is None:
            return []

        decisions: list[SharedDecision] = []
        for key in context.list_decisions():
            decision = context.get_decision(key)
            if decision:
                decisions.append(
                    SharedDecision(
                        key=decision.key,
                        value=decision.value,
                        rationale=decision.rationale,
                        decided_by=str(decision.decided_by),
                    )
                )
        return decisions
