"""Context value objects for child-to-parent communication.

These immutable value objects enable rich result passing from child to parent:
- TaskOutcome: Structured result from child back to parent
"""

from typing import Any, Self

from pydantic import BaseModel


class TaskOutcome(BaseModel):
    """Structured result from child to parent.

    Provides rich feedback beyond just the result text:
    - Task summary: What task was assigned to the child
    - Result text: The actual output/result
    - Artifacts: Keys of artifacts stored in shared context
    - Decisions: Key decisions made during execution
    - Context updates: Updates to propagate to shared context
    - Execution summary: Cost, duration, tokens used
    """

    model_config = {"frozen": True}

    task_summary: str = ""
    result_text: str
    artifacts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    context_updates: dict[str, Any] = {}
    execution_summary: dict[str, Any] = {}

    @classmethod
    def simple(cls, result_text: str, task_summary: str = "") -> Self:
        """Create a simple result with just text."""
        return cls(result_text=result_text, task_summary=task_summary)
