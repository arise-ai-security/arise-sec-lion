"""Context value objects for parent-child communication.

These immutable value objects enable rich context passing between agents:
- ParentContext: Context passed from parent to child at spawn time
- ChildResult: Structured result from child back to parent
- AncestorInfo: Lightweight ancestor summary for ancestry chain
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID


if TYPE_CHECKING:
    from core.domain.model import AgentSession


@dataclass(frozen=True, slots=True)
class AncestorInfo:
    """Lightweight ancestor summary for ancestry chain.

    Provides minimal context about each ancestor in the hierarchy,
    enabling children to understand their position and lineage.
    """

    agent_id: str
    role: str
    task_summary: str  # First 100 chars of task description

    @classmethod
    def from_agent(cls, agent: AgentSession) -> AncestorInfo:
        """Create AncestorInfo from an AgentSession."""
        task_summary = (agent.task_description or "")[:100]
        return cls(
            agent_id=str(agent.agent_id),
            role=agent.role.value,
            task_summary=task_summary,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "agent_id": self.agent_id,
            "role": self.role,
            "task_summary": self.task_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AncestorInfo:
        """Deserialize from dictionary."""
        return cls(
            agent_id=data["agent_id"],
            role=data["role"],
            task_summary=data["task_summary"],
        )


@dataclass(frozen=True, slots=True)
class ParentContext:
    """Context passed from parent to child at spawn time.

    Provides children with:
    - Parent's task and role for understanding context
    - Depth in hierarchy for limit enforcement
    - Ancestry chain for debugging and decision-making
    - Parent's decisions to maintain consistency
    - Constraints inherited from the hierarchy
    - Execution limits (budget, depth remaining, etc.)
    """

    parent_task: str
    parent_role: str
    depth: int
    ancestry: tuple[AncestorInfo, ...]
    decisions: tuple[str, ...]
    constraints: dict[str, Any]
    execution_limits: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for event storage."""
        return {
            "parent_task": self.parent_task,
            "parent_role": self.parent_role,
            "depth": self.depth,
            "ancestry": [a.to_dict() for a in self.ancestry],
            "decisions": list(self.decisions),
            "constraints": self.constraints,
            "execution_limits": self.execution_limits,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ParentContext:
        """Deserialize from dictionary."""
        return cls(
            parent_task=data["parent_task"],
            parent_role=data["parent_role"],
            depth=data["depth"],
            ancestry=tuple(AncestorInfo.from_dict(a) for a in data.get("ancestry", [])),
            decisions=tuple(data.get("decisions", [])),
            constraints=data.get("constraints", {}),
            execution_limits=data.get("execution_limits", {}),
        )


@dataclass(frozen=True, slots=True)
class ChildResult:
    """Structured result from child to parent.

    Provides rich feedback beyond just the result text:
    - Result text: The actual output/result
    - Artifacts: Keys of artifacts stored in shared context
    - Decisions: Key decisions made during execution
    - Context updates: Updates to propagate to shared context
    - Execution summary: Cost, duration, tokens used
    """

    result_text: str
    artifacts: tuple[str, ...]
    decisions: tuple[str, ...]
    context_updates: dict[str, Any]
    execution_summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for event storage."""
        return {
            "result_text": self.result_text,
            "artifacts": list(self.artifacts),
            "decisions": list(self.decisions),
            "context_updates": self.context_updates,
            "execution_summary": self.execution_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildResult:
        """Deserialize from dictionary."""
        return cls(
            result_text=data["result_text"],
            artifacts=tuple(data.get("artifacts", [])),
            decisions=tuple(data.get("decisions", [])),
            context_updates=data.get("context_updates", {}),
            execution_summary=data.get("execution_summary", {}),
        )

    @classmethod
    def simple(cls, result_text: str) -> ChildResult:
        """Create a simple result with just text."""
        return cls(
            result_text=result_text,
            artifacts=(),
            decisions=(),
            context_updates={},
            execution_summary={},
        )


def build_parent_context(
    agent: AgentSession,
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
