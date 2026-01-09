"""Prompt parser for extracting sections with provenance.

Extracts XML-tagged sections from prompt text and classifies them
by their provenance (source). No visual formatting logic.
"""

import re

from core.domain.values.prompt_trace import PromptSection, SectionProvenance


class PromptParser:
    """Extracts ALL sections from prompt text with provenance classification.

    Dynamically parses any XML tag found in the prompt, then classifies
    by provenance using explicit rules or pattern-based inference.
    """

    # Explicit mapping from tag to provenance (case-insensitive lookup)
    # Organized by the 3-layer prompt architecture:
    #   Layer 1: Core Behavior (TEMPLATE) - Agent mechanics from core/roles/*.j2
    #   Layer 2: Shared Context (PARENT, SIBLING, CHILDREN, SHARED)
    #   Layer 3: Domain-Specific (SYSTEM) - SEC-bench CVE data, constraints
    PROVENANCE_RULES: dict[str, SectionProvenance] = {
        # =================================================================
        # Layer 1: Core Agent Behavior (TEMPLATE)
        # Static template content from core/roles/*.j2, core/strategies/*.j2
        # =================================================================
        "role": SectionProvenance.TEMPLATE,
        "task": SectionProvenance.TEMPLATE,
        "task_description": SectionProvenance.TEMPLATE,
        "task_to_evaluate": SectionProvenance.TEMPLATE,
        "task_context": SectionProvenance.TEMPLATE,
        "decision_guide": SectionProvenance.TEMPLATE,
        "output_format": SectionProvenance.TEMPLATE,
        "guidelines": SectionProvenance.TEMPLATE,
        "capabilities": SectionProvenance.TEMPLATE,
        "complexity_evaluation_strategy": SectionProvenance.TEMPLATE,
        "decomposition_strategy": SectionProvenance.TEMPLATE,
        "strategic_thinking": SectionProvenance.TEMPLATE,
        "config": SectionProvenance.TEMPLATE,
        "config_guide": SectionProvenance.TEMPLATE,
        "constraints": SectionProvenance.TEMPLATE,
        "success_criteria": SectionProvenance.TEMPLATE,
        "instructions": SectionProvenance.TEMPLATE,
        "worker_instructions": SectionProvenance.TEMPLATE,
        # Research findings from RESEARCHER phase (becomes template context)
        "research_findings": SectionProvenance.TEMPLATE,
        "research-findings": SectionProvenance.TEMPLATE,
        # =================================================================
        # Layer 2: Shared Context (PARENT, SIBLING, CHILDREN, SHARED)
        # Context passed between agents in the hierarchy
        # =================================================================
        # Parent sections (from SpawnPayload)
        "parent-context": SectionProvenance.PARENT,
        "parent_context": SectionProvenance.PARENT,
        "parent_task": SectionProvenance.PARENT,
        "ancestry": SectionProvenance.PARENT,
        "thinker_justification": SectionProvenance.PARENT,
        # Sibling sections (from SiblingViewBuilder)
        "sibling-tasks": SectionProvenance.SIBLING,
        "sibling_context": SectionProvenance.SIBLING,
        "coworker_knowledge": SectionProvenance.SIBLING,
        # Children sections (from ChildOutcomes)
        "child-outcomes": SectionProvenance.CHILDREN,
        "children": SectionProvenance.CHILDREN,
        # Shared sections (from SharedExecutionContext)
        "global-context": SectionProvenance.SHARED,
        "shared-decisions": SectionProvenance.SHARED,
        "shared_decisions": SectionProvenance.SHARED,
        "shared-artifacts": SectionProvenance.SHARED,
        "shared_artifacts": SectionProvenance.SHARED,
        "context-update": SectionProvenance.SHARED,
        "context-update-instructions": SectionProvenance.SHARED,
        "decisions": SectionProvenance.SHARED,
        "artifacts": SectionProvenance.SHARED,
        # =================================================================
        # Layer 3: Domain-Specific (SYSTEM)
        # SEC-bench CVE data, environment, constraints from secbench/*.j2
        # =================================================================
        # SEC-bench CVE instance (secbench/context/instance.j2)
        "cve_instance": SectionProvenance.SYSTEM,
        "cve-context": SectionProvenance.SYSTEM,
        "bug_description": SectionProvenance.SYSTEM,
        "bug_report": SectionProvenance.SYSTEM,
        # SEC-bench environment (secbench/context/environment.j2)
        "repository_info": SectionProvenance.SYSTEM,
        "work_dir": SectionProvenance.SYSTEM,
        "sanitizer": SectionProvenance.SYSTEM,
        "base_commit_hash": SectionProvenance.SYSTEM,
        "commit_hash": SectionProvenance.SYSTEM,
        "commit_hash1": SectionProvenance.SYSTEM,
        "commit_hash2": SectionProvenance.SYSTEM,
        "commit_url": SectionProvenance.SYSTEM,
        "changed_file_path": SectionProvenance.SYSTEM,
        # Workspace and hierarchy
        "workspace": SectionProvenance.SYSTEM,
        "workspace-context": SectionProvenance.SYSTEM,
        "hierarchy-limits": SectionProvenance.SYSTEM,
        "instance": SectionProvenance.SYSTEM,
        "source_context": SectionProvenance.SYSTEM,
        "security_context": SectionProvenance.SYSTEM,
        "issue_description": SectionProvenance.SYSTEM,
        "files": SectionProvenance.SYSTEM,
        "uploaded_files": SectionProvenance.SYSTEM,
        # User prompt (from core/context/user_prompt.j2)
        "user_prompt": SectionProvenance.SYSTEM,
    }

    # Pattern-based inference for unknown tags
    PROVENANCE_PATTERNS: list[tuple[str, SectionProvenance]] = [
        (r"parent|ancestry|thinker", SectionProvenance.PARENT),
        (r"sibling|coworker|peer", SectionProvenance.SIBLING),
        (r"child|outcome", SectionProvenance.CHILDREN),
        (r"shared|global|context-update|decision|artifact", SectionProvenance.SHARED),
        (r"cve|security|workspace|instance|commit|file|bug|sanitizer|user_prompt", SectionProvenance.SYSTEM),
    ]

    def parse(self, raw_prompt: str) -> tuple[PromptSection, ...]:
        """Extract ALL sections with their provenance from prompt text.

        Dynamically finds all XML tags and classifies each by provenance.

        Args:
            raw_prompt: Raw prompt text containing XML sections.

        Returns:
            Tuple of PromptSection objects ordered by appearance in text.
        """
        sections: list[tuple[int, PromptSection]] = []

        # Match all XML tags: <tag>content</tag> or <tag attr="x">content</tag>
        pattern = r"<([A-Za-z_][A-Za-z0-9_-]*)(?:\s[^>]*)?>(.+?)</\1>"

        for match in re.finditer(pattern, raw_prompt, re.DOTALL):
            tag = match.group(1)
            content = match.group(2).strip()
            position = match.start()

            if not content:
                continue

            provenance = self._classify_provenance(tag)
            sections.append((
                position,
                PromptSection(tag=tag, content=content, provenance=provenance),
            ))

        # Sort by position in original text
        sections.sort(key=lambda x: x[0])
        return tuple(s for _, s in sections)

    def _classify_provenance(self, tag: str) -> SectionProvenance:
        """Classify a tag's provenance using rules then pattern inference.

        Args:
            tag: XML tag name.

        Returns:
            SectionProvenance classification.
        """
        # Normalize: lowercase, treat hyphens and underscores as equivalent
        normalized = tag.lower().replace("-", "_")

        # 1. Check explicit rules
        if normalized in self.PROVENANCE_RULES:
            return self.PROVENANCE_RULES[normalized]

        # Also check with hyphen variant
        hyphen_variant = normalized.replace("_", "-")
        if hyphen_variant in self.PROVENANCE_RULES:
            return self.PROVENANCE_RULES[hyphen_variant]

        # 2. Pattern-based inference
        for pattern, provenance in self.PROVENANCE_PATTERNS:
            if re.search(pattern, normalized):
                return provenance

        # 3. Default: UPPERCASE tags are typically TEMPLATE, lowercase are nested/data
        if tag.isupper():
            return SectionProvenance.TEMPLATE

        # 4. Fallback to TEMPLATE (most common)
        return SectionProvenance.TEMPLATE

    def extract_section(self, raw_prompt: str, tag: str) -> str | None:
        """Extract a specific section's content by tag name.

        Args:
            raw_prompt: Raw prompt text.
            tag: Section tag to extract (e.g., "ROLE", "parent-context").

        Returns:
            Section content or None if not found.
        """
        pattern = rf"<{tag}(?:\s[^>]*)?>(.+?)</{tag}>"
        match = re.search(pattern, raw_prompt, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else None
