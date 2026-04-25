"""Child agent factory for creating agents from ChildSpawned events.

Handles child agent creation with hierarchy limits propagation.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from core.domain.aggregates.agent_session import AgentRole, AgentSession
from core.domain.events.events import ChildSpawned


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

    def reset(self, initial_count: int = 0) -> None:
        """Reset factory state for a new run."""
        self._total_created = initial_count

    @property
    def total_created(self) -> int:
        """Get total number of agents created."""
        return self._total_created

    @property
    def max_total_agents(self) -> int:
        """Get the max total agents limit (-1 means unlimited)."""
        return self._max_total_agents

    def is_at_agent_limit(self) -> bool:
        """Check if we've reached the max_total_agents limit."""
        if self._max_total_agents < 0:
            return False
        return self._total_created >= self._max_total_agents

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
        manager_api_base = getattr(self._manager_config, "api_base", None)

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

    async def create_from_event(
        self,
        event: ChildSpawned,
        parent_id: UUID,
    ) -> ChildCreationResult:
        """Create a child agent from a ChildSpawned event.

        Limit checking is done by CheckLimitViolations pipeline step before
        events are emitted. Propagates hierarchy limits and spawn payload to child.
        Applies manager config defaults for PENDING children.
        """
        child_role = AgentRole(event.child_role)
        child_config = event.child_config

        # Apply manager config to override LLM-generated model names
        # This ensures the configured model always takes precedence over
        # potentially invalid model names generated by the LLM (e.g., "claude-3-5-sonnet"
        # without date suffix). Applied to all roles, not just PENDING.
        child_config = self._apply_manager_config(child_config)

        # Pass briefing to create() so it's persisted in AgentCreated event
        # This ensures briefing is restored when agent is loaded from history
        child = AgentSession.create(
            agent_id=event.child_id,
            role=child_role,
            config=child_config,
            parent_id=parent_id,
            sibling_index=event.sibling_index,
            briefing=event.briefing,
            depends_on=event.subtask.depends_on,
            success_criteria=event.subtask.success_criteria,
            target_paths=list(event.subtask.target_paths),
            symbols=list(event.subtask.symbols),
            search_hints=list(event.subtask.search_hints),
            estimated_complexity=event.subtask.estimated_complexity,
        )
        child.assign_task(event.subtask.description)

        self._limits_registry.propagate_to_child(parent_id, event.child_id)
        await self._repository.save_new_agent(child)
        self._total_created += 1

        return ChildCreationResult(agent=child, created=True)

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

        # Create all child agents in parallel using asyncio.gather
        # This is safe because each event creates an independent agent
        results = await asyncio.gather(
            *[self._create_child_parallel(event, parent_id) for event in events]
        )

        # Update counter after all parallel operations complete
        # This avoids race conditions on _total_created
        self._total_created += len(events)

        return list(results)

    async def _create_child_parallel(
        self,
        event: ChildSpawned,
        parent_id: UUID,
    ) -> ChildCreationResult:
        """Create a single child agent (for parallel execution).

        Does NOT increment _total_created - caller handles that after gather.
        """
        child_role = AgentRole(event.child_role)
        child_config = self._apply_manager_config(event.child_config)

        child = AgentSession.create(
            agent_id=event.child_id,
            role=child_role,
            config=child_config,
            parent_id=parent_id,
            sibling_index=event.sibling_index,
            briefing=event.briefing,
            depends_on=event.subtask.depends_on,
            success_criteria=event.subtask.success_criteria,
            target_paths=list(event.subtask.target_paths),
            symbols=list(event.subtask.symbols),
            search_hints=list(event.subtask.search_hints),
            estimated_complexity=event.subtask.estimated_complexity,
        )
        child.assign_task(event.subtask.description)

        self._limits_registry.propagate_to_child(parent_id, event.child_id)

        # Persist the new agent (parallel DB write)
        await self._repository.save_new_agent(child)

        return ChildCreationResult(agent=child, created=True)
