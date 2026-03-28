"""Prompt parser for extracting sections with provenance."""

import re

from core.domain.values.prompt_trace import PromptSection, SectionProvenance


class PromptParser:
    """Extract XML-tagged prompt sections and classify their provenance."""

    BASE_PROVENANCE_RULES: dict[str, SectionProvenance] = {
        "system": SectionProvenance.TEMPLATE,
        "persona": SectionProvenance.TEMPLATE,
        "operation": SectionProvenance.TEMPLATE,
        "domain": SectionProvenance.SYSTEM,
        "domain_operation": SectionProvenance.TEMPLATE,
        "domain_instructions": SectionProvenance.TEMPLATE,
        "config_reference": SectionProvenance.TEMPLATE,
        "environment": SectionProvenance.SYSTEM,
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
        "briefing": SectionProvenance.PARENT,
        "parent_context": SectionProvenance.PARENT,
        "parent_task": SectionProvenance.PARENT,
        "ancestry": SectionProvenance.PARENT,
        "thinker_justification": SectionProvenance.PARENT,
        "sibling_tasks": SectionProvenance.SIBLING,
        "sibling_context": SectionProvenance.SIBLING,
        "coworker_knowledge": SectionProvenance.SIBLING,
        "child_outcomes": SectionProvenance.CHILDREN,
        "children": SectionProvenance.CHILDREN,
        "global_context": SectionProvenance.SHARED,
        "shared_decisions": SectionProvenance.SHARED,
        "shared_artifacts": SectionProvenance.SHARED,
        "context_update": SectionProvenance.SHARED,
        "context_update_instructions": SectionProvenance.SHARED,
        "decisions": SectionProvenance.SHARED,
        "artifacts": SectionProvenance.SHARED,
        "hierarchy_limits": SectionProvenance.SYSTEM,
        "workspace": SectionProvenance.SYSTEM,
        "workspace_context": SectionProvenance.SYSTEM,
        "instance": SectionProvenance.SYSTEM,
        "source_context": SectionProvenance.SYSTEM,
        "work_dir": SectionProvenance.SYSTEM,
        "commit_hash": SectionProvenance.SYSTEM,
        "commit_hash1": SectionProvenance.SYSTEM,
        "commit_hash2": SectionProvenance.SYSTEM,
        "commit_url": SectionProvenance.SYSTEM,
        "changed_file_path": SectionProvenance.SYSTEM,
        "user_prompt": SectionProvenance.SYSTEM,
        "files": SectionProvenance.SYSTEM,
        "uploaded_files": SectionProvenance.SYSTEM,
    }

    BASE_PROVENANCE_PATTERNS: list[tuple[str, SectionProvenance]] = [
        (r"parent|ancestry|thinker", SectionProvenance.PARENT),
        (r"sibling|coworker|peer", SectionProvenance.SIBLING),
        (r"child|outcome", SectionProvenance.CHILDREN),
        (r"shared|global|context-update|decision|artifact", SectionProvenance.SHARED),
        (r"workspace|user_prompt", SectionProvenance.SYSTEM),
    ]
    HISTORICAL_DOMAIN_PATTERNS: list[tuple[str, SectionProvenance]] = [
        (r"cve|security|sanitizer|bug_report|exploit", SectionProvenance.SYSTEM),
    ]

    def __init__(
        self,
        extra_tag_mappings: dict[str, SectionProvenance] | None = None,
        extra_provenance_patterns: list[tuple[str, SectionProvenance]] | None = None,
    ) -> None:
        self._provenance_rules = dict(self.BASE_PROVENANCE_RULES)
        if extra_tag_mappings:
            self._provenance_rules.update(
                {
                    key.lower().replace("-", "_"): value
                    for key, value in extra_tag_mappings.items()
                }
            )

        self._provenance_patterns = list(self.BASE_PROVENANCE_PATTERNS)
        if extra_provenance_patterns:
            self._provenance_patterns.extend(extra_provenance_patterns)
        self._provenance_patterns.extend(self.HISTORICAL_DOMAIN_PATTERNS)

    def parse(self, raw_prompt: str) -> tuple[PromptSection, ...]:
        """Extract all tagged sections ordered by appearance."""
        sections: list[tuple[int, PromptSection]] = []
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

        sections.sort(key=lambda x: x[0])
        return tuple(section for _, section in sections)

    def _classify_provenance(self, tag: str) -> SectionProvenance:
        normalized = tag.lower().replace("-", "_")

        if normalized in self._provenance_rules:
            return self._provenance_rules[normalized]

        hyphen_variant = normalized.replace("_", "-")
        if hyphen_variant in self._provenance_rules:
            return self._provenance_rules[hyphen_variant]

        for pattern, provenance in self._provenance_patterns:
            if re.search(pattern, normalized):
                return provenance

        if tag.isupper():
            return SectionProvenance.TEMPLATE

        return SectionProvenance.TEMPLATE

    def extract_section(self, raw_prompt: str, tag: str) -> str | None:
        """Extract a specific section's content by tag name."""
        pattern = rf"<{tag}(?:\s[^>]*)?>(.+?)</{tag}>"
        match = re.search(pattern, raw_prompt, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else None
