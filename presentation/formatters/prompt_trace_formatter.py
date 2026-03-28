"""Prompt trace formatters for rendering hierarchy traces.

Contains all visual formatting logic (colors, icons, indentation).
Domain layer has no knowledge of these presentation details.
"""

import json
from abc import ABC, abstractmethod
from typing import Any

from core.domain.values.prompt_trace import (
    AgentNode,
    HierarchyTrace,
    ParsedPrompt,
    RenderOptions,
    SectionProvenance,
)


class PromptTraceRenderer(ABC):
    """Abstract base for hierarchy trace renderers."""

    @abstractmethod
    def render(self, trace: HierarchyTrace, options: RenderOptions) -> str:
        """Render the hierarchy trace to a string."""


class TreeRenderer(PromptTraceRenderer):
    """Renders HierarchyTrace as colored tree with provenance indicators.

    All visual logic (colors, icons, indentation) is contained here.
    """

    # Provenance display styles: (emoji, label)
    PROVENANCE_STYLES: dict[SectionProvenance, tuple[str, str]] = {
        SectionProvenance.TEMPLATE: ("🔵", "BASE TEMPLATE"),
        SectionProvenance.PARENT: ("🟢", "FROM PARENT"),
        SectionProvenance.SIBLING: ("🟡", "FROM SIBLINGS"),
        SectionProvenance.CHILDREN: ("🟣", "FROM CHILDREN"),
        SectionProvenance.SHARED: ("🟠", "SHARED"),
        SectionProvenance.SYSTEM: ("🔴", "SYSTEM"),
    }

    # Role icons
    ROLE_ICONS: dict[str, str] = {
        "boss": "👔",
        "manager": "📋",
        "worker": "⚙️",
        "pending": "⏳",
    }

    SEPARATOR = "=" * 60

    def render(self, trace: HierarchyTrace, options: RenderOptions) -> str:
        """Render hierarchy as formatted tree string."""
        lines: list[str] = []

        # Header
        lines.append(self.SEPARATOR)
        lines.append(f"PROMPT TRACE: {trace.total_agents} agents, max_depth={trace.max_depth}")
        lines.append(self.SEPARATOR)
        lines.append("")

        # Render tree
        self._render_node(trace.root, lines, indent=0, options=options)

        return "\n".join(lines)

    def _render_node(
        self,
        node: AgentNode,
        lines: list[str],
        indent: int,
        options: RenderOptions,
    ) -> None:
        """Render single agent node with its prompts."""
        # Apply filters
        if options.max_depth is not None and node.depth > options.max_depth:
            return
        if options.filter_role is not None and node.role != options.filter_role:
            # Still render children that might match
            for child in node.children:
                self._render_node(child, lines, indent, options)
            return

        prefix = "  " * indent
        role_icon = self.ROLE_ICONS.get(node.role, "❓")
        short_id = str(node.agent_id)[:8]

        # Agent header
        lines.append(f"{prefix}{self.SEPARATOR}")
        sibling_info = f" (sibling_index={node.sibling_index})" if node.sibling_index > 0 else ""
        lines.append(f"{prefix}{role_icon} [{node.role.upper()}] depth={node.depth}{sibling_info}")
        lines.append(f"{prefix}   Agent: {short_id}...")
        task_preview = node.task[:60] + "..." if len(node.task) > 60 else node.task
        lines.append(f"{prefix}   Task: {task_preview}")

        # Render prompts
        for i, prompt in enumerate(node.prompts, 1):
            self._render_prompt(prompt, lines, prefix, i, options)

        # Spawn info
        if node.children:
            lines.append(f"{prefix}   📤 SPAWNED: {len(node.children)} children")

        lines.append("")

        # Render children
        for child in node.children:
            self._render_node(child, lines, indent + 1, options)

    def _render_prompt(
        self,
        prompt: ParsedPrompt,
        lines: list[str],
        prefix: str,
        index: int,
        options: RenderOptions,
    ) -> None:
        """Render a single prompt with sections grouped by provenance."""
        timestamp = prompt.occurred_at.strftime("%Y-%m-%dT%H:%M:%S")
        lines.append(f"{prefix}   ┌─ Prompt #{index} ({timestamp}) [{prompt.prompt_type}] → {prompt.target}")
        lines.append(f"{prefix}   │")

        # Group sections by provenance
        for provenance in SectionProvenance:
            sections = prompt.by_provenance(provenance)
            if not sections:
                continue

            # Apply section filter if specified
            if options.section_filter:
                sections = [s for s in sections if s.tag == options.section_filter]
                if not sections:
                    continue

            emoji, label = self.PROVENANCE_STYLES[provenance]
            lines.append(f"{prefix}   │  {emoji} {label} " + "─" * 40)

            for section in sections:
                # Show full content - no truncation
                for line in section.content.split("\n"):
                    lines.append(f"{prefix}   │  <{section.tag}>{line}")

            lines.append(f"{prefix}   │")

        lines.append(f"{prefix}   └─ ({prompt.raw_length} chars)")


class JsonRenderer(PromptTraceRenderer):
    """Renders HierarchyTrace as structured JSON."""

    def render(self, trace: HierarchyTrace, options: RenderOptions) -> str:
        """Render hierarchy as JSON string."""
        data = {
            "total_agents": trace.total_agents,
            "max_depth": trace.max_depth,
            "root": self._node_to_dict(trace.root, options),
        }
        return json.dumps(data, indent=2, default=str)

    def _node_to_dict(self, node: AgentNode, options: RenderOptions) -> dict[str, Any]:
        """Convert AgentNode to dictionary."""
        # Apply filters
        if options.max_depth is not None and node.depth > options.max_depth:
            return {}
        if options.filter_role is not None and node.role != options.filter_role:
            # Return dict with only children that match
            children = [
                c for c in (self._node_to_dict(child, options) for child in node.children) if c
            ]
            if children:
                return {"filtered_children": children}
            return {}

        return {
            "agent_id": str(node.agent_id),
            "role": node.role,
            "depth": node.depth,
            "task": node.task,
            "sibling_index": node.sibling_index,
            "prompts": [self._prompt_to_dict(p, options) for p in node.prompts],
            "children": [self._node_to_dict(child, options) for child in node.children],
        }

    def _prompt_to_dict(self, prompt: ParsedPrompt, options: RenderOptions) -> dict[str, Any]:
        """Convert ParsedPrompt to dictionary."""
        sections_by_provenance: dict[str, list[dict]] = {}

        for section in prompt.sections:
            # Apply section filter
            if options.section_filter and section.tag != options.section_filter:
                continue

            prov_key = section.provenance.value
            if prov_key not in sections_by_provenance:
                sections_by_provenance[prov_key] = []

            sections_by_provenance[prov_key].append({
                "tag": section.tag,
                "content": section.content,
            })

        return {
            "occurred_at": prompt.occurred_at.isoformat(),
            "prompt_type": prompt.prompt_type,
            "target": prompt.target,
            "raw_length": prompt.raw_length,
            "sections": sections_by_provenance,
        }


class SiblingFlowRenderer(PromptTraceRenderer):
    """Renders sibling data flow view - shows what each worker received from siblings."""

    def render(self, trace: HierarchyTrace, options: RenderOptions) -> str:
        """Render sibling flow view."""
        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("SIBLING WORKER DATA FLOW")
        lines.append("=" * 70)
        lines.append("")

        self._render_managers_with_workers(trace.root, lines, options)

        return "\n".join(lines)

    def _render_managers_with_workers(
        self,
        node: AgentNode,
        lines: list[str],
        options: RenderOptions,
    ) -> None:
        """Find managers/boss with worker children and show sibling flow."""
        # Check if this node has worker children
        worker_children = [c for c in node.children if c.role == "worker"]

        if worker_children:
            short_id = str(node.agent_id)[:8]
            task_preview = node.task[:50] + "..." if len(node.task) > 50 else node.task
            lines.append(f"📋 Parent {node.role.upper()}: {short_id}...")
            lines.append(f"   Task: {task_preview}")
            lines.append(f"   Workers: {len(worker_children)}")
            lines.append("")

            for worker in worker_children:
                self._render_worker_sibling_data(worker, lines, options)

            lines.append("")

        # Recurse to children
        for child in node.children:
            self._render_managers_with_workers(child, lines, options)

    def _render_worker_sibling_data(
        self,
        worker: AgentNode,
        lines: list[str],
        options: RenderOptions,
    ) -> None:
        """Render what sibling data this worker received."""
        short_id = str(worker.agent_id)[:8]
        task_preview = worker.task[:40] + "..." if len(worker.task) > 40 else worker.task

        lines.append(f"   ⚙️  Worker #{worker.sibling_index + 1}: {short_id}...")
        lines.append(f"      Task: {task_preview}")

        # Find sibling-tasks section in prompts
        for prompt in worker.prompts:
            sibling_sections = prompt.by_provenance(SectionProvenance.SIBLING)
            if sibling_sections:
                for section in sibling_sections:
                    # Extract summary info from sibling-tasks content
                    content = section.content
                    preview = content[:200] + "..." if len(content) > 200 else content
                    lines.append("      📥 Received sibling data:")
                    for line in preview.split("\n")[:5]:
                        lines.append(f"         {line}")
                break
        else:
            lines.append("      📥 Received: No sibling data (first worker)")

        lines.append("")


def get_renderer(format_type: str) -> PromptTraceRenderer:
    """Factory function to get appropriate renderer."""
    renderers: dict[str, PromptTraceRenderer] = {
        "tree": TreeRenderer(),
        "json": JsonRenderer(),
        "siblings": SiblingFlowRenderer(),
    }
    return renderers.get(format_type, TreeRenderer())
