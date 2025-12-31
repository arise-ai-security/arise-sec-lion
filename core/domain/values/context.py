"""Context value objects for parent-child communication.

These immutable value objects enable rich context passing between agents:
- ParentContext: Context passed from parent to child at spawn time
- ChildResult: Structured result from child back to parent
- AncestorInfo: Lightweight ancestor summary for ancestry chain
"""



from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel

if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession


class AncestorInfo(BaseModel):
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
        """Create AncestorInfo from an AgentSession."""
        task_summary = (agent.task_description or "")[:100]
        return cls(
            agent_id=str(agent.agent_id),
            role=agent.role.value,
            task_summary=task_summary,
        )


class ParentContext(BaseModel):
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
    ancestry: tuple[AncestorInfo, ...]
    decisions: tuple[str, ...]
    constraints: dict[str, Any]
    execution_limits: dict[str, Any]


class ChildResult(BaseModel):
    """Structured result from child to parent.

    Provides rich feedback beyond just the result text:
    - Result text: The actual output/result
    - Artifacts: Keys of artifacts stored in shared context
    - Decisions: Key decisions made during execution
    - Context updates: Updates to propagate to shared context
    - Execution summary: Cost, duration, tokens used
    """

    model_config = {"frozen": True}

    result_text: str
    artifacts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    context_updates: dict[str, Any] = {}
    execution_summary: dict[str, Any] = {}

    @classmethod
    def simple(cls, result_text: str) -> Self:
        """Create a simple result with just text."""
        return cls(result_text=result_text)


def build_parent_context(
    agent: "AgentSession",
    parent_context: ParentContext | None = None,
) -> ParentContext:
    """Build ParentContext from agent for passing to children.

    Constructs the full ancestry chain by appending the current agent
    to the parent's ancestry.

    Args:
        agent: The parent agent spawning children
        parent_context: The context this agent received from its parent

    Returns:
        ParentContext to pass to child agents
    """
    # Build ancestry chain
    if parent_context is not None:
        ancestry = list(parent_context.ancestry)
    else:
        ancestry = []
    ancestry.append(AncestorInfo.from_agent(agent))

    # Get depth from execution context
    depth = 0
    if agent.execution_context is not None:
        depth = agent.execution_context.current_depth

    # Build execution limits
    execution_limits: dict[str, Any] = {}
    if agent.execution_context is not None:
        execution_limits = {
            "depth_remaining": agent.execution_context.depth_remaining(),
            "max_children_per_node": agent.execution_context.max_children_per_node,
            "max_retries": agent.execution_context.max_retries,
            "agents_remaining": agent.execution_context.agents_remaining(),
            "max_total_agents": agent.execution_context.max_total_agents,
        }
        # Add root_id if available
        if hasattr(agent.execution_context, "root_id"):
            execution_limits["root_id"] = str(agent.execution_context.root_id)

    # Get local decisions from agent
    local_decisions: tuple[str, ...] = ()
    if hasattr(agent, "local_decisions"):
        local_decisions = tuple(agent.local_decisions)

    return ParentContext(
        parent_task=agent.task_description or "",
        parent_role=agent.role.value,
        depth=depth,
        ancestry=tuple(ancestry),
        decisions=local_decisions,
        constraints={},  # TODO: Implement constraint inheritance
        execution_limits=execution_limits,
    )
