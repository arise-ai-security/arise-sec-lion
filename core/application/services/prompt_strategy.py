"""Prompt strategy protocol and generic default implementation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.values.enums import AgentRole

if TYPE_CHECKING:
    from core.domain.values.limits import HierarchyLimits
    from core.domain.values.node_message import Briefing


@dataclass(frozen=True, slots=True)
class SubtaskScope:
    """Structured scoping info from parent's subtask decomposition."""

    target_paths: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    search_hints: tuple[str, ...] = ()

    @property
    def has_scope(self) -> bool:
        return bool(self.target_paths or self.symbols or self.search_hints)


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Immutable context for prompt strategies."""

    task_description: str
    agent_id: UUID
    agent_role: AgentRole
    default_tool: str
    parent_task: str | None = None
    briefing: "Briefing | None" = None
    hierarchy_limits: "HierarchyLimits | None" = None
    domain_context: object | None = None
    handoff: Any = None
    workspace_context: str | None = None
    scope: SubtaskScope | None = None


class PromptStrategy(Protocol):
    """Strategy interface for prompt building.

    Implementations return custom prompts for specific use cases,
    or None to fall back to default prompt building.
    """

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        """Return custom boss prompt, or None to use default."""
        ...

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        """Return custom manager prompt, or None to use default."""
        ...

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        """Return custom worker prompt, or None to use default."""
        ...

    def build_assessment_prompt(self, context: PromptContext) -> str | None:
        """Return custom assessment prompt, or None to use default."""
        ...


class DefaultPromptStrategy:
    """Default strategy - returns None to use base prompts for all roles."""

    def build_boss_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_manager_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_worker_prompt(self, context: PromptContext) -> str | None:
        return None

    def build_assessment_prompt(self, context: PromptContext) -> str | None:
        return None
