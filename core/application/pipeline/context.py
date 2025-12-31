"""Pipeline state and result types.

This module defines the immutable data structures used for communication
between pipeline steps:

- PipelineState: Carries state through the pipeline, delegates to domain types
- StepResult: Represents success (with updated state) or failure (with reason)
"""

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.values.cve_instance import CVEInstance
    from core.domain.values.context import HierarchyLimits, SiblingView
    from core.domain.values.llm_response import LLMResponse
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.services import RegisteredTask
    from core.domain.values.subtask import Subtask


@dataclass(frozen=True, slots=True)
class PipelineState:
    """Immutable state passed through pipeline steps.

    This is an ephemeral transport object that carries state between steps
    within a single pipeline execution. It delegates to the domain layer's
    HierarchyLimits for limit tracking to avoid duplication.

    Design decisions:
    - Immutable (frozen=True) for safe passing between steps
    - Delegates to agent.hierarchy_limits for limits (no duplication)
    - Uses with_* methods to create modified copies
    - Only carries ephemeral step-to-step state
    """

    agent: "AgentSession"

    # Step-to-step ephemeral state (NOT duplicated from HierarchyLimits)
    prompt: str | None = None
    llm_response: "LLMResponse | None" = None
    operation: str = ""

    # Decomposition-specific
    registered_tasks: "list[RegisteredTask] | None" = None
    subtasks: "list[Subtask] | None" = None
    child_role: str | None = None
    force_worker: bool = False

    # Worker-specific
    working_directory: str | None = None
    workspace_context: str | None = None
    sibling_view: "SiblingView | None" = None

    # Parsing results
    complexity: str | None = None
    reasoning: str | None = None

    # DELEGATION: Access limits via existing domain HierarchyLimits
    @property
    def hierarchy_limits(self) -> "HierarchyLimits | None":
        """Get the agent's hierarchy limits (limit tracking)."""
        return self.agent.hierarchy_limits

    @property
    def cve_instance(self) -> "CVEInstance | None":
        """Get CVE instance from hierarchy limits."""
        limits = self.hierarchy_limits
        return limits.cve_instance if limits else None

    @property
    def root_id(self) -> UUID | None:
        """Get root agent ID from hierarchy limits."""
        limits = self.hierarchy_limits
        return limits.root_id if limits else None

    # Builder methods for immutable updates
    def with_prompt(self, prompt: str) -> "PipelineState":
        """Create new state with prompt set."""
        return replace(self, prompt=prompt)

    def with_llm_response(self, response: "LLMResponse") -> "PipelineState":
        """Create new state with LLM response set."""
        return replace(self, llm_response=response)

    def with_registered_tasks(self, tasks: "list[RegisteredTask]") -> "PipelineState":
        """Create new state with registered tasks set."""
        return replace(self, registered_tasks=tasks)

    def with_subtasks(self, subtasks: "list[Subtask]") -> "PipelineState":
        """Create new state with parsed subtasks set."""
        return replace(self, subtasks=subtasks)

    def with_child_role(self, role: str, force_worker: bool = False) -> "PipelineState":
        """Create new state with child role determined."""
        return replace(self, child_role=role, force_worker=force_worker)

    def with_complexity_result(
        self, complexity: str, reasoning: str
    ) -> "PipelineState":
        """Create new state with complexity evaluation result."""
        return replace(self, complexity=complexity, reasoning=reasoning)

    def with_worker_context(
        self,
        working_directory: str | None = None,
        workspace_context: str | None = None,
        sibling_view: "SiblingView | None" = None,
    ) -> "PipelineState":
        """Create new state with worker execution parameters."""
        return replace(
            self,
            working_directory=working_directory,
            workspace_context=workspace_context,
            sibling_view=sibling_view,
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    """Result of a pipeline step execution.

    Either success with updated state, or failure with reason.
    Uses factory methods for clarity and type safety.

    Usage:
        # On success, return updated state
        return StepResult.ok(state.with_prompt(new_prompt))

        # On failure, return reason
        return StepResult.fail("Invalid agent role")
    """

    success: bool
    state: "PipelineState | None" = None
    failure_reason: str | None = None

    @classmethod
    def ok(cls, state: "PipelineState") -> "StepResult":
        """Create success result with updated state."""
        return cls(success=True, state=state)

    @classmethod
    def fail(cls, reason: str) -> "StepResult":
        """Create failure result with reason."""
        return cls(success=False, failure_reason=reason)
