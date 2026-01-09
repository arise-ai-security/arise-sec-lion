"""Domain value objects for prompt tracing.

Provides pure domain types for representing prompt sections, their provenance,
and the agent hierarchy structure. No visual formatting or database logic.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID


class SectionProvenance(str, Enum):
    """Source of a prompt section - determines color coding in display."""

    TEMPLATE = "template"  # Base .j2 templates (ROLE, TASK, DECISION_GUIDE)
    PARENT = "parent"  # From parent via SpawnPayload (parent-context, ancestry)
    SIBLING = "sibling"  # From sibling workers (sibling-tasks)
    CHILDREN = "children"  # From completed children (child-outcomes)
    SHARED = "shared"  # From SharedExecutionContext (global-context, decisions)
    SYSTEM = "system"  # Hierarchy limits, CVE, workspace


@dataclass(frozen=True)
class PromptSection:
    """Single extracted section from a prompt.

    Represents a tagged XML section within a prompt, with its provenance
    indicating where the content originated (template, parent, sibling, etc.).
    """

    tag: str  # e.g., "ROLE", "parent-context", "sibling-tasks"
    content: str
    provenance: SectionProvenance


@dataclass(frozen=True)
class ParsedPrompt:
    """Prompt with sections grouped by provenance.

    Contains both the raw prompt text and the parsed sections for
    structured display and analysis.
    """

    raw: str
    occurred_at: datetime
    prompt_type: str  # "complexity_evaluation", "task_decomposition", "worker_execution"
    target: str  # "llm" or tool name like "claude_code"
    sections: tuple[PromptSection, ...]

    def by_provenance(self, provenance: SectionProvenance) -> list[PromptSection]:
        """Get all sections with a specific provenance."""
        return [s for s in self.sections if s.provenance == provenance]

    @property
    def raw_length(self) -> int:
        """Length of the raw prompt text."""
        return len(self.raw)


@dataclass
class AgentNode:
    """Agent with its prompts and children in the hierarchy.

    Represents a single node in the agent tree, containing the agent's
    metadata, parsed prompts, and child nodes.
    """

    agent_id: UUID
    role: str  # "boss", "manager", "worker", "pending", "researcher"
    depth: int
    task: str
    sibling_index: int
    prompts: tuple[ParsedPrompt, ...]
    children: tuple["AgentNode", ...] = ()

    @property
    def prompt_count(self) -> int:
        """Number of prompts sent by this agent."""
        return len(self.prompts)


@dataclass(frozen=True)
class HierarchyTrace:
    """Complete hierarchy trace from a root agent.

    Contains the full tree of agents with their parsed prompts,
    plus summary statistics about the hierarchy.
    """

    root: AgentNode
    total_agents: int
    max_depth: int


@dataclass(frozen=True)
class RenderOptions:
    """Options for rendering the hierarchy trace.

    Controls filtering, depth limits, and output format.
    """

    max_depth: int | None = None  # None = show all depths
    filter_role: str | None = None  # None = show all roles
    section_filter: str | None = None  # Only show specific section tag
