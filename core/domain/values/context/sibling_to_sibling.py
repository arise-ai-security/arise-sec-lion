"""Sibling context value objects for worker execution.

Immutable value objects enabling workers to see:
- Parent task context
- Sibling task statuses and results
- Shared decisions from SharedExecutionContext
"""

from typing import Any

from pydantic import BaseModel, computed_field


class SiblingStatus(BaseModel):
    """Sibling task status for worker coordination."""

    model_config = {"frozen": True}

    agent_id: str
    sibling_index: int
    status: str  # pending, analyzing, in_progress, completed, failed
    task_summary: str
    result_summary: str | None = None


class SharedDecision(BaseModel):
    """Shared decision visible to sibling workers."""

    model_config = {"frozen": True}

    key: str
    value: str
    rationale: str = ""
    decided_by: str = ""


class SiblingView(BaseModel):
    """Complete sibling view passed to worker prompts."""

    model_config = {"frozen": True}

    current_agent_id: str
    parent_task: str | None
    sibling_tasks: tuple[SiblingStatus, ...] = ()
    shared_decisions: tuple[SharedDecision, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_siblings(self) -> int:
        return len(self.sibling_tasks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.sibling_tasks if s.status == "completed")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_progress_count(self) -> int:
        return sum(
            1 for s in self.sibling_tasks if s.status in ("analyzing", "in_progress")
        )

    def to_template_dict(self) -> dict[str, Any]:
        """Convert to dict for Jinja2 template rendering (includes computed fields)."""
        return self.model_dump()
