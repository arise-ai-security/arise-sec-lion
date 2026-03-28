"""Prompt strategy protocol for optional prompt-chain extensions."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.values.enums import AgentRole
from core.domain.values.prompt_capabilities import PromptCapabilities


if TYPE_CHECKING:
    from core.application.services.prompt.prompt_builder import TemplateChain
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
    prompt_capabilities: PromptCapabilities | None = None


class PromptStrategy(Protocol):
    """Strategy interface for prompt building.

    Implementations may extend the default prompt chain for specific use cases,
    or return None to use the default chain unchanged.
    """

    def extend_assessment_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        """Return an extended assessment prompt chain, or None to use the default chain."""
        ...

    def extend_boss_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        """Return an extended boss prompt chain, or None to use the default chain."""
        ...

    def extend_manager_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        """Return an extended manager prompt chain, or None to use the default chain."""
        ...

    def extend_worker_prompt(
        self,
        chain: "TemplateChain",
        context: PromptContext,
    ) -> "TemplateChain | None":
        """Return an extended worker prompt chain, or None to use the default chain."""
        ...
