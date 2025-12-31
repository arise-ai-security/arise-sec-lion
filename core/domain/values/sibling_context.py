"""Sibling context value objects for worker execution.

Immutable value objects enabling workers to see:
- Parent task context
- Sibling task statuses and results
- Shared decisions from SharedExecutionContext
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Self


@dataclass(frozen=True, slots=True)
class SiblingTaskInfo:
    """Sibling task info for worker coordination."""

    agent_id: str
    sibling_index: int
    status: str  # pending, analyzing, in_progress, completed, failed
    task_summary: str
    result_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**{k: v for k, v in data.items() if k in cls.__slots__})


@dataclass(frozen=True, slots=True)
class DecisionInfo:
    """Shared decision visible to sibling workers."""

    key: str
    value: str
    rationale: str = ""
    decided_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**{k: v for k, v in data.items() if k in cls.__slots__})


@dataclass(frozen=True, slots=True)
class WorkerSiblingContext:
    """Complete sibling context passed to worker prompts."""

    current_agent_id: str
    parent_task: str | None
    sibling_tasks: tuple[SiblingTaskInfo, ...] = ()
    shared_decisions: tuple[DecisionInfo, ...] = ()

    @property
    def total_siblings(self) -> int:
        return len(self.sibling_tasks)

    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.sibling_tasks if s.status == "completed")

    @property
    def in_progress_count(self) -> int:
        return sum(1 for s in self.sibling_tasks if s.status in ("analyzing", "in_progress"))

    def to_template_dict(self) -> dict[str, Any]:
        """Convert to dict for Jinja2 template rendering."""
        return {
            **asdict(self),
            "sibling_tasks": [s.to_dict() for s in self.sibling_tasks],
            "shared_decisions": [d.to_dict() for d in self.shared_decisions],
            "total_siblings": self.total_siblings,
            "completed_count": self.completed_count,
            "in_progress_count": self.in_progress_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_agent_id": self.current_agent_id,
            "parent_task": self.parent_task,
            "sibling_tasks": [s.to_dict() for s in self.sibling_tasks],
            "shared_decisions": [d.to_dict() for d in self.shared_decisions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            current_agent_id=data["current_agent_id"],
            parent_task=data.get("parent_task"),
            sibling_tasks=tuple(SiblingTaskInfo.from_dict(s) for s in data.get("sibling_tasks", [])),
            shared_decisions=tuple(DecisionInfo.from_dict(d) for d in data.get("shared_decisions", [])),
        )
