"""Context value objects for parent-to-child communication.

These immutable value objects enable rich context passing from parent to child:
- SpawnPayload: Context passed from parent to child at spawn time
- AncestorSummary: Lightweight ancestor summary for ancestry chain
"""

from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel

if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession


class AncestorSummary(BaseModel):
    """Lightweight ancestor summary for ancestry chain.

    Provides minimal context about each ancestor in the hierarchy,
    enabling children to understand their position and lineage.
    """

    model_config = {"frozen": True}

    agent_id: str
    role: str
    task_summary: str  # First 100 chars of task description

    @classmethod
    def from_agent(cls, agent: "AgentSession") -> Self:
        """Create AncestorSummary from an AgentSession."""
        task_summary = (agent.task_description or "")[:100]
        return cls(
            agent_id=str(agent.agent_id),
            role=agent.role.value,
            task_summary=task_summary,
        )


class SpawnPayload(BaseModel):
    """Context passed from parent to child at spawn time.

    Provides children with:
    - Parent's task and role for understanding context
    - Depth in hierarchy for limit enforcement
    - Ancestry chain for debugging and decision-making
    - Parent's decisions to maintain consistency
    - Constraints inherited from the hierarchy
    - Execution limits (budget, depth remaining, etc.)
    """

    model_config = {"frozen": True}

    parent_task: str
    parent_role: str
    depth: int
    ancestry: tuple[AncestorSummary, ...]
    decisions: tuple[str, ...]
    constraints: dict[str, Any]
    execution_limits: dict[str, Any]


def build_spawn_payload(
    agent: "AgentSession",
    parent_payload: SpawnPayload | None = None,
) -> SpawnPayload:
    """Build SpawnPayload from agent for passing to children.

    Constructs the full ancestry chain by appending the current agent
    to the parent's ancestry.

    Args:
        agent: The parent agent spawning children
        parent_payload: The payload this agent received from its parent

    Returns:
        SpawnPayload to pass to child agents
    """
    # Build ancestry chain
    if parent_payload is not None:
        ancestry = list(parent_payload.ancestry)
    else:
        ancestry = []
    ancestry.append(AncestorSummary.from_agent(agent))

    # Get depth from hierarchy limits
    depth = 0
    if agent.hierarchy_limits is not None:
        depth = agent.hierarchy_limits.current_depth

    # Build execution limits
    execution_limits: dict[str, Any] = {}
    if agent.hierarchy_limits is not None:
        execution_limits = {
            "depth_remaining": agent.hierarchy_limits.depth_remaining(),
            "max_children_per_node": agent.hierarchy_limits.max_children_per_node,
            "max_retries": agent.hierarchy_limits.max_retries,
            "agents_remaining": agent.hierarchy_limits.agents_remaining(),
            "max_total_agents": agent.hierarchy_limits.max_total_agents,
        }
        # Add root_id if available
        if hasattr(agent.hierarchy_limits, "root_id"):
            execution_limits["root_id"] = str(agent.hierarchy_limits.root_id)

    # Get local decisions from agent
    local_decisions: tuple[str, ...] = ()
    if hasattr(agent, "local_decisions"):
        local_decisions = tuple(agent.local_decisions)

    return SpawnPayload(
        parent_task=agent.task_description or "",
        parent_role=agent.role.value,
        depth=depth,
        ancestry=tuple(ancestry),
        decisions=local_decisions,
        constraints={},  # TODO: Implement constraint inheritance
        execution_limits=execution_limits,
    )
