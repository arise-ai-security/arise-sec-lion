"""Resolved toolset context shared by prompt rendering and LLM querying."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.domain.values.prompt_capabilities import (
    PromptCapabilities,
    PromptToolDescriptor,
)


if TYPE_CHECKING:
    from core.ports.runtime_ports import Toolset


@dataclass(frozen=True, slots=True)
class ToolsetPolicy:
    """Per-toolset policy resolved for a single query."""

    enabled: bool = True
    allowed_tools: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class LoopPolicy:
    """Shared limits for a single tool-calling loop."""

    max_iterations: int = 5
    result_char_limit: int = 6_000


@dataclass(frozen=True, slots=True)
class ActiveToolContext:
    """Resolved snapshot of the active toolsets for one LLM query."""

    tool_definitions: tuple[dict[str, Any], ...] = ()
    loop_policy: LoopPolicy = LoopPolicy()
    executors: Mapping[str, Toolset] = field(default_factory=dict)

    @property
    def has_tools(self) -> bool:
        """Whether the current query exposes any tools to the LLM."""
        return bool(self.tool_definitions)


def build_prompt_capabilities(
    tool_context: ActiveToolContext | None,
) -> PromptCapabilities:
    """Convert active runtime tools into prompt-safe capability descriptors."""
    if tool_context is None or not tool_context.has_tools:
        return PromptCapabilities()

    available_tools = tuple(
        descriptor
        for tool_def in tool_context.tool_definitions
        if (descriptor := PromptToolDescriptor.from_tool_definition(tool_def)) is not None
    )
    return PromptCapabilities(available_tools=available_tools)
