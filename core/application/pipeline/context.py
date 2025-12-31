"""Pipeline context and result types.

This module defines the immutable data structures used for communication
between pipeline steps:

- PipelineContext: Carries state through the pipeline, delegates to domain contexts
- StepResult: Represents success (with updated context) or failure (with reason)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from core.domain.cve_instance import CVEInstance
    from core.domain.execution_context import ExecutionContext
    from core.domain.llm_response import LLMResponse
    from core.domain.model import AgentSession
    from core.domain.services import RegisteredTask
    from core.domain.sibling_context import WorkerSiblingContext
    from core.domain.subtask import Subtask


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Immutable context passed through pipeline steps.

    This is an ephemeral transport object that carries state between steps
    within a single pipeline execution. It delegates to the domain layer's
    ExecutionContext for limit tracking to avoid duplication.

    Design decisions:
    - Immutable (frozen=True) for safe passing between steps
    - Delegates to agent.execution_context for limits (no duplication)
    - Uses with_* methods to create modified copies
    - Only carries ephemeral step-to-step state
    """

    agent: AgentSession

    # Step-to-step ephemeral state (NOT duplicated from ExecutionContext)
    prompt: str | None = None
    llm_response: LLMResponse | None = None
    operation: str = ""

    # Decomposition-specific
    registered_tasks: list[RegisteredTask] | None = None
    subtasks: list[Subtask] | None = None
    child_role: str | None = None
    force_worker: bool = False

    # Worker-specific
    working_directory: str | None = None
    workspace_context: str | None = None
    sibling_context: WorkerSiblingContext | None = None

    # Parsing results
    complexity: str | None = None
    reasoning: str | None = None

    # DELEGATION: Access limits via existing domain ExecutionContext
    @property
    def execution_context(self) -> ExecutionContext | None:
        """Get the agent's execution context (limit tracking)."""
        return self.agent.execution_context

    @property
    def cve_instance(self) -> CVEInstance | None:
        """Get CVE instance from execution context."""
        ctx = self.execution_context
        return ctx.cve_instance if ctx else None

    @property
    def root_id(self) -> UUID | None:
        """Get root agent ID from execution context."""
        ctx = self.execution_context
        return ctx.root_id if ctx else None

    # Builder methods for immutable updates
    def with_prompt(self, prompt: str) -> PipelineContext:
        """Create new context with prompt set."""
        return replace(self, prompt=prompt)

    def with_llm_response(self, response: LLMResponse) -> PipelineContext:
        """Create new context with LLM response set."""
        return replace(self, llm_response=response)

    def with_registered_tasks(self, tasks: list[RegisteredTask]) -> PipelineContext:
        """Create new context with registered tasks set."""
        return replace(self, registered_tasks=tasks)

    def with_subtasks(self, subtasks: list[Subtask]) -> PipelineContext:
        """Create new context with parsed subtasks set."""
        return replace(self, subtasks=subtasks)

    def with_child_role(self, role: str, force_worker: bool = False) -> PipelineContext:
        """Create new context with child role determined."""
        return replace(self, child_role=role, force_worker=force_worker)

    def with_complexity_result(
        self, complexity: str, reasoning: str
    ) -> PipelineContext:
        """Create new context with complexity evaluation result."""
        return replace(self, complexity=complexity, reasoning=reasoning)

    def with_worker_context(
        self,
        working_directory: str | None = None,
        workspace_context: str | None = None,
        sibling_context: WorkerSiblingContext | None = None,
    ) -> PipelineContext:
        """Create new context with worker execution parameters."""
        return replace(
            self,
            working_directory=working_directory,
            workspace_context=workspace_context,
            sibling_context=sibling_context,
        )


@dataclass(frozen=True, slots=True)
class StepResult:
    """Result of a pipeline step execution.

    Either success with updated context, or failure with reason.
    Uses factory methods for clarity and type safety.

    Usage:
        # On success, return updated context
        return StepResult.ok(ctx.with_prompt(new_prompt))

        # On failure, return reason
        return StepResult.fail("Invalid agent role")
    """

    success: bool
    context: PipelineContext | None = None
    failure_reason: str | None = None

    @classmethod
    def ok(cls, ctx: PipelineContext) -> StepResult:
        """Create success result with updated context."""
        return cls(success=True, context=ctx)

    @classmethod
    def fail(cls, reason: str) -> StepResult:
        """Create failure result with reason."""
        return cls(success=False, failure_reason=reason)
