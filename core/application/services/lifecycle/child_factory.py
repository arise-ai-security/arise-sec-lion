"""Child agent factory for creating agents from ChildSpawned events.

Handles child agent creation with hierarchy limits propagation.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import ChildSpawned

_BRACKET_PREFIX = re.compile(r"^\[([^\]]+)\]")


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from config import ManagerConfig
    from core.application.services.lifecycle.agent_repository import AgentRepository
    from core.application.services.lifecycle.hierarchy_limits_registry import (
        HierarchyLimitsRegistry,
    )


@dataclass
class ChildCreationResult:
    """Result of child agent creation."""

    agent: AgentSession
    created: bool  # False if limit was reached


class ChildAgentFactory:
    """Factory for creating child agents from ChildSpawned events.

    Single Responsibility: Create child agents with proper limits propagation.

    Provides:
    - Agent count tracking (source of truth for max_total_agents enforcement)
    - Hierarchy limits propagation to children
    - Manager config defaults for PENDING children

    Note: Limit enforcement is done by CheckLimitViolations pipeline step,
    which queries this factory for real-time agent counts.
    """

    def __init__(
        self,
        repository: "AgentRepository",
        limits_registry: "HierarchyLimitsRegistry",
        max_total_agents: int = -1,  # -1 means unlimited
        manager_config: "ManagerConfig | None" = None,
        default_worker_tool: str | None = None,
    ) -> None:
        self._repository = repository
        self._limits_registry = limits_registry
        self._max_total_agents = max_total_agents
        self._manager_config = manager_config
        self._default_worker_tool = default_worker_tool
        self._total_created: int = 0
        # Atomic reservation guard (D.2). Concurrent MANAGER evaluations
        # must reserve under the lock before emitting ChildSpawned events
        # so the cap check and the commit observe a consistent count.
        self._lock = asyncio.Lock()
        self._reserved: int = 0

    def reset(self, initial_count: int = 0) -> None:
        """Reset factory state for a new run."""
        self._total_created = initial_count
        self._reserved = 0

    @property
    def total_created(self) -> int:
        return self._total_created

    @property
    def max_total_agents(self) -> int:
        """Get the max total agents limit (-1 means unlimited)."""
        return self._max_total_agents

    @property
    def reserved(self) -> int:
        return self._reserved

    def is_at_agent_limit(self) -> bool:
        if self._max_total_agents < 0:
            return False
        return self._total_created >= self._max_total_agents

    async def try_reserve(self, n: int) -> bool:
        """Atomically reserve ``n`` agent slots against ``max_total_agents``.

        Returns True if the reservation succeeded; False if the cap would
        be exceeded. The caller must eventually call ``commit_reservation``
        (on success) or ``release_reservation`` (on failure / OCC retry)
        with the same ``n``.

        With ``max_total_agents == -1`` (unlimited), the reservation
        always succeeds and is not actually tracked.
        """
        if self._max_total_agents < 0:
            return True
        async with self._lock:
            projected = self._total_created + self._reserved + n
            if projected > self._max_total_agents:
                return False
            self._reserved += n
            return True

    async def release_reservation(self, n: int) -> None:
        """Release ``n`` previously-reserved slots without committing.

        Called on the OCC-retry branch when a managed spawn attempt fails
        and must be retried; the orchestrator releases before reloading
        so the retry sees the full available cap again.
        """
        if self._max_total_agents < 0 or n <= 0:
            return
        async with self._lock:
            self._reserved = max(0, self._reserved - n)

    async def commit_reservation(self, n: int) -> None:
        """Atomically move ``n`` slots from reserved to committed.

        Always bumps ``_total_created`` so the observed count tracks real
        spawns even in unlimited mode (where reservations are not held).
        """
        if n <= 0:
            return
        async with self._lock:
            if self._max_total_agents >= 0:
                self._reserved = max(0, self._reserved - n)
            self._total_created += n

    def _apply_manager_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Apply manager config defaults to child config.

        Overrides LLM-generated model names and tool with configured values.
        The worker tool is always forced to the configured default — LLMs must
        not choose the execution tool.
        Handles both heuristic/hybrid (base) and per_operation strategies.
        """
        # Deep copy to avoid mutating original
        result = dict(config)

        # Always enforce the configured worker tool, regardless of what the
        # LLM generated.  Qwen-family models frequently hallucinate tool
        # names (e.g., "claude_code") that don't match the deployment config.
        if self._default_worker_tool is not None:
            result["tool"] = self._default_worker_tool

        if self._manager_config is None:
            return result

        manager_model = self._manager_config.model
        manager_temp = self._manager_config.temperature
        manager_max_tokens = self._manager_config.max_tokens
        manager_api_base = self._manager_config.api_base

        # Handle heuristic/hybrid strategy (has "base" field)
        if "base" in result and isinstance(result["base"], dict):
            result["base"] = dict(result["base"])
            result["base"]["model"] = manager_model
            result["base"]["temperature"] = manager_temp
            result["base"]["max_tokens"] = manager_max_tokens
            result["base"]["api_base"] = manager_api_base

        # Handle per_operation strategy (has complexity_evaluation, task_decomposition)
        for op_key in ("complexity_evaluation", "task_decomposition"):
            if op_key in result and isinstance(result[op_key], dict):
                result[op_key] = dict(result[op_key])
                result[op_key]["model"] = manager_model
                result[op_key]["temperature"] = manager_temp
                result[op_key]["max_tokens"] = manager_max_tokens
                result[op_key]["api_base"] = manager_api_base

        return result

    async def create_children_from_events(
        self,
        events: list[ChildSpawned],
        parent_id: UUID,
    ) -> list[ChildCreationResult]:
        """Create multiple child agents from ChildSpawned events.

        Limit checking is done by CheckLimitViolations pipeline step before
        this method is called. This method assumes all events are valid.

        Optimized: Uses asyncio.gather for parallel DB writes instead of
        sequential awaits. With N children, reduces latency from N*RTT to ~1*RTT.

        Returns:
            List of created children in same order as input events.
        """
        if not events:
            return []

        # Resolve each child's depends_on (sibling indices) to the concrete
        # predecessor agent ids so a worker's context packet can be scoped to
        # exactly its hard predecessors.
        sibling_map = {event.sibling_index: event.child_id for event in events}

        # Create all child agents in parallel using asyncio.gather
        # This is safe because each event creates an independent agent
        results = await asyncio.gather(
            *[self._create_child_parallel(event, parent_id, sibling_map) for event in events]
        )

        # Commit the reservation atomically (moves from _reserved to
        # _total_created under the lock when the factory is bounded).
        await self.commit_reservation(len(events))

        return list(results)

    async def _create_child_parallel(
        self,
        event: ChildSpawned,
        parent_id: UUID,
        sibling_map: dict[int, UUID],
    ) -> ChildCreationResult:
        """Create a single child agent (for parallel execution).

        Does NOT increment _total_created - caller handles that after gather.
        """
        child_role = AgentRole(event.child_role)
        child_config = self._apply_manager_config(event.child_config)
        hard_predecessor_ids = [
            sibling_map[index]
            for index in event.subtask.depends_on
            if index in sibling_map
        ]

        child = AgentSession.create(
            agent_id=event.child_id,
            role=child_role,
            config=child_config,
            parent_id=parent_id,
            sibling_index=event.sibling_index,
            briefing=event.briefing,
            depends_on=event.subtask.depends_on,
            hard_predecessor_ids=hard_predecessor_ids,
            success_criteria=event.subtask.success_criteria,
            criticality=event.subtask.criticality,
            dependency_failure_policy=event.subtask.dependency_failure_policy,
            target_paths=list(event.subtask.target_paths),
            symbols=list(event.subtask.symbols),
            search_hints=list(event.subtask.search_hints),
            estimated_complexity=event.subtask.estimated_complexity,
            execution_mode=event.subtask.execution_mode,
            procedure_ref=event.subtask.procedure_ref,
            procedure_params=dict(event.subtask.procedure_params),
        )
        child.assign_task(event.subtask.description)

        self._limits_registry.propagate_to_child(parent_id, event.child_id)

        # Register role prefix for cross-tree dedup
        m = _BRACKET_PREFIX.match(event.subtask.description.strip())
        if m:
            self._limits_registry.register_role(
                event.child_id, m.group(1), parent_id=parent_id,
            )

        # Persist the new agent (parallel DB write)
        await self._repository.save_new_agent(child)

        return ChildCreationResult(agent=child, created=True)
