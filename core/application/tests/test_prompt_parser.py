"""Unit tests for PromptParser.

Tests the dynamic XML tag extraction and provenance classification.
"""

import pytest

from core.application.services.prompt_parser import PromptParser
from core.domain.values.prompt_trace import SectionProvenance


class TestPromptParserBasicExtraction:
    """Tests for basic XML tag extraction."""

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_extracts_simple_tag(self, parser: PromptParser) -> None:
        """Test extraction of a simple XML tag."""
        prompt = "<ROLE>You are a BOSS agent.</ROLE>"
        sections = parser.parse(prompt)

        assert len(sections) == 1
        assert sections[0].tag == "ROLE"
        assert sections[0].content == "You are a BOSS agent."

    def test_extracts_multiple_tags(self, parser: PromptParser) -> None:
        """Test extraction of multiple different tags."""
        prompt = """
        <ROLE>Boss agent</ROLE>
        <TASK>Do something</TASK>
        <CONSTRAINTS>Don't break things</CONSTRAINTS>
        """
        sections = parser.parse(prompt)

        assert len(sections) == 3
        tags = [s.tag for s in sections]
        assert "ROLE" in tags
        assert "TASK" in tags
        assert "CONSTRAINTS" in tags

    def test_extracts_multiline_content(self, parser: PromptParser) -> None:
        """Test extraction of multiline tag content."""
        prompt = """<ROLE>
You are a BOSS agent.
Hierarchy: BOSS → MANAGER → WORKER
Primary responsibility: Coordination.
</ROLE>"""
        sections = parser.parse(prompt)

        assert len(sections) == 1
        assert "BOSS agent" in sections[0].content
        assert "MANAGER" in sections[0].content
        assert "Coordination" in sections[0].content

    def test_extracts_tag_with_attributes(self, parser: PromptParser) -> None:
        """Test extraction of tags with XML attributes."""
        prompt = '<knowledge key="test" relevance="high">Important info</knowledge>'
        sections = parser.parse(prompt)

        assert len(sections) == 1
        assert sections[0].tag == "knowledge"
        assert sections[0].content == "Important info"

    def test_preserves_order_by_position(self, parser: PromptParser) -> None:
        """Test that sections are ordered by their position in the prompt."""
        prompt = """
        <TASK>First task</TASK>
        <ROLE>Second role</ROLE>
        <CONSTRAINTS>Third constraints</CONSTRAINTS>
        """
        sections = parser.parse(prompt)

        assert sections[0].tag == "TASK"
        assert sections[1].tag == "ROLE"
        assert sections[2].tag == "CONSTRAINTS"

    def test_ignores_empty_tags(self, parser: PromptParser) -> None:
        """Test that empty tags are not included."""
        prompt = """
        <ROLE>Has content</ROLE>
        <TASK></TASK>
        <CONSTRAINTS>   </CONSTRAINTS>
        """
        sections = parser.parse(prompt)

        # Only ROLE has non-whitespace content
        assert len(sections) == 1
        assert sections[0].tag == "ROLE"

    def test_handles_nested_tags(self, parser: PromptParser) -> None:
        """Test handling of nested XML structures.

        Note: The regex uses non-greedy matching, so nested tags with the same
        structure are extracted as the outermost match. Inner tags are included
        in the content of the outer tag.
        """
        prompt = """
        <COWORKER_KNOWLEDGE count="2">
        <knowledge key="test">Inner content</knowledge>
        </COWORKER_KNOWLEDGE>
        """
        sections = parser.parse(prompt)

        # Outer tag is extracted, inner content is included
        tags = [s.tag for s in sections]
        assert "COWORKER_KNOWLEDGE" in tags
        # The inner knowledge tag's content is part of COWORKER_KNOWLEDGE content
        outer_section = next(s for s in sections if s.tag == "COWORKER_KNOWLEDGE")
        assert "<knowledge" in outer_section.content

    def test_handles_special_characters_in_content(self, parser: PromptParser) -> None:
        """Test handling of special characters in tag content."""
        prompt = "<TASK>Fix bug #123 & handle edge cases < 100</TASK>"
        sections = parser.parse(prompt)

        assert len(sections) == 1
        assert "#123" in sections[0].content
        assert "&" in sections[0].content


class TestPromptParserProvenanceClassification:
    """Tests for provenance classification of tags."""

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_template_tags_uppercase(self, parser: PromptParser) -> None:
        """Test that UPPERCASE template tags are classified as TEMPLATE."""
        template_tags = [
            "ROLE", "TASK", "TASK_DESCRIPTION", "DECISION_GUIDE",
            "OUTPUT_FORMAT", "CAPABILITIES", "CONSTRAINTS", "GUIDELINES",
        ]
        for tag in template_tags:
            prompt = f"<{tag}>content</{tag}>"
            sections = parser.parse(prompt)
            assert sections[0].provenance == SectionProvenance.TEMPLATE, f"Failed for {tag}"

    def test_parent_context_tags(self, parser: PromptParser) -> None:
        """Test that parent context tags are classified as PARENT."""
        parent_tags = ["parent-context", "THINKER_JUSTIFICATION", "ancestry"]
        for tag in parent_tags:
            prompt = f"<{tag}>content</{tag}>"
            sections = parser.parse(prompt)
            assert sections[0].provenance == SectionProvenance.PARENT, f"Failed for {tag}"

    def test_sibling_tags(self, parser: PromptParser) -> None:
        """Test that sibling tags are classified as SIBLING."""
        sibling_tags = ["sibling-tasks", "COWORKER_KNOWLEDGE", "SIBLING_CONTEXT"]
        for tag in sibling_tags:
            prompt = f"<{tag}>content</{tag}>"
            sections = parser.parse(prompt)
            assert sections[0].provenance == SectionProvenance.SIBLING, f"Failed for {tag}"

    def test_shared_context_tags(self, parser: PromptParser) -> None:
        """Test that shared context tags are classified as SHARED."""
        shared_tags = ["shared-decisions", "shared-artifacts", "context-update", "decisions"]
        for tag in shared_tags:
            prompt = f"<{tag}>content</{tag}>"
            sections = parser.parse(prompt)
            assert sections[0].provenance == SectionProvenance.SHARED, f"Failed for {tag}"

    def test_system_tags(self, parser: PromptParser) -> None:
        """Test that system tags are classified as SYSTEM."""
        system_tags = [
            "cve_instance", "user_prompt", "workspace", "SOURCE_CONTEXT",
            "work_dir", "commit_hash", "sanitizer",
        ]
        for tag in system_tags:
            prompt = f"<{tag}>content</{tag}>"
            sections = parser.parse(prompt)
            assert sections[0].provenance == SectionProvenance.SYSTEM, f"Failed for {tag}"

    def test_hyphen_underscore_equivalence(self, parser: PromptParser) -> None:
        """Test that hyphens and underscores are treated equivalently."""
        # Both should be classified the same
        prompt1 = "<parent-context>content</parent-context>"
        prompt2 = "<parent_context>content</parent_context>"

        sections1 = parser.parse(prompt1)
        sections2 = parser.parse(prompt2)

        assert sections1[0].provenance == sections2[0].provenance == SectionProvenance.PARENT

    def test_case_insensitive_classification(self, parser: PromptParser) -> None:
        """Test that classification is case-insensitive."""
        prompts = [
            "<ROLE>content</ROLE>",
            "<role>content</role>",
            "<Role>content</Role>",
        ]
        for prompt in prompts:
            sections = parser.parse(prompt)
            assert sections[0].provenance == SectionProvenance.TEMPLATE


class TestPromptParserPatternInference:
    """Tests for pattern-based provenance inference for unknown tags."""

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_parent_pattern_inference(self, parser: PromptParser) -> None:
        """Test that tags containing 'parent' are inferred as PARENT."""
        prompt = "<new_parent_info>content</new_parent_info>"
        sections = parser.parse(prompt)
        assert sections[0].provenance == SectionProvenance.PARENT

    def test_sibling_pattern_inference(self, parser: PromptParser) -> None:
        """Test that tags containing 'sibling' are inferred as SIBLING."""
        prompt = "<sibling_results>content</sibling_results>"
        sections = parser.parse(prompt)
        assert sections[0].provenance == SectionProvenance.SIBLING

    def test_cve_pattern_inference(self, parser: PromptParser) -> None:
        """Test that tags containing 'cve' are inferred as SYSTEM."""
        prompt = "<cve_details>content</cve_details>"
        sections = parser.parse(prompt)
        assert sections[0].provenance == SectionProvenance.SYSTEM

    def test_unknown_uppercase_defaults_to_template(self, parser: PromptParser) -> None:
        """Test that unknown UPPERCASE tags default to TEMPLATE."""
        prompt = "<UNKNOWN_TAG>content</UNKNOWN_TAG>"
        sections = parser.parse(prompt)
        assert sections[0].provenance == SectionProvenance.TEMPLATE

    def test_unknown_lowercase_defaults_to_template(self, parser: PromptParser) -> None:
        """Test that unknown lowercase tags default to TEMPLATE."""
        prompt = "<random_tag>content</random_tag>"
        sections = parser.parse(prompt)
        assert sections[0].provenance == SectionProvenance.TEMPLATE


class TestPromptParserExtractSection:
    """Tests for the extract_section helper method."""

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_extract_existing_section(self, parser: PromptParser) -> None:
        """Test extracting a section that exists."""
        prompt = "<ROLE>Boss agent</ROLE><TASK>Do work</TASK>"
        content = parser.extract_section(prompt, "ROLE")
        assert content == "Boss agent"

    def test_extract_nonexistent_section(self, parser: PromptParser) -> None:
        """Test extracting a section that doesn't exist."""
        prompt = "<ROLE>Boss agent</ROLE>"
        content = parser.extract_section(prompt, "TASK")
        assert content is None

    def test_extract_section_case_insensitive(self, parser: PromptParser) -> None:
        """Test that extraction is case-insensitive."""
        prompt = "<ROLE>Boss agent</ROLE>"
        content = parser.extract_section(prompt, "role")
        assert content == "Boss agent"


class TestPromptParserRealWorldPrompts:
    """Tests using real-world prompt patterns from the prompts/ directory."""

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_boss_decomposition_prompt(self, parser: PromptParser) -> None:
        """Test parsing a typical BOSS decomposition prompt."""
        prompt = """
<ROLE>You are a **BOSS** agent - the root coordinator in a recursive multi-agent hierarchy.
Hierarchy: BOSS (you) → MANAGER → WORKER
Primary responsibility: Strategic task analysis and delegation.</ROLE>

<CAPABILITIES>- Analyze task complexity and requirements
- Decompose tasks into 2-3 concrete subtasks
- Configure child agents (model, temperature, tool)
- Coordinate children and synthesize results</CAPABILITIES>

<CONSTRAINTS>- NEVER execute tasks yourself; delegate to children
- Each subtask MUST be independently executable
- Optimal: 2-3 subtasks (avoid over-decomposition)</CONSTRAINTS>

<cve_instance>- Instance Id: exiv2.cve-2017-14857
- Repo: exiv2/exiv2
- Sanitizer: address</cve_instance>

<user_prompt>Fix the CVE</user_prompt>
"""
        sections = parser.parse(prompt)

        # Verify all expected sections are found
        tags = {s.tag for s in sections}
        assert "ROLE" in tags
        assert "CAPABILITIES" in tags
        assert "CONSTRAINTS" in tags
        assert "cve_instance" in tags
        assert "user_prompt" in tags

        # Verify provenance
        provenance_map = {s.tag: s.provenance for s in sections}
        assert provenance_map["ROLE"] == SectionProvenance.TEMPLATE
        assert provenance_map["cve_instance"] == SectionProvenance.SYSTEM
        assert provenance_map["user_prompt"] == SectionProvenance.SYSTEM

    def test_worker_prompt_with_sibling_context(self, parser: PromptParser) -> None:
        """Test parsing a WORKER prompt with sibling context."""
        prompt = """
<ROLE>You are a WORKER agent. Execute the task directly.</ROLE>

<TASK>Build the project with ASAN</TASK>

<THINKER_JUSTIFICATION>Your thinker (parent agent) assigned this task with the following context:

**Thinker's Original Task**: Fix the CVE
**Budget Allocation**: 33% of budget (weight 1.0 of 3.0 across 3 subtasks)</THINKER_JUSTIFICATION>

<COWORKER_KNOWLEDGE count="2">
<knowledge key="builder">Build completed successfully</knowledge>
</COWORKER_KNOWLEDGE>

<sibling-tasks>You are worker 2 of 3. 1 completed, 2 in progress.</sibling-tasks>
"""
        sections = parser.parse(prompt)

        # Verify provenance classification
        provenance_map = {s.tag: s.provenance for s in sections}
        assert provenance_map["ROLE"] == SectionProvenance.TEMPLATE
        assert provenance_map["TASK"] == SectionProvenance.TEMPLATE
        assert provenance_map["THINKER_JUSTIFICATION"] == SectionProvenance.PARENT
        assert provenance_map["COWORKER_KNOWLEDGE"] == SectionProvenance.SIBLING
        assert provenance_map["sibling-tasks"] == SectionProvenance.SIBLING

    def test_complexity_evaluation_prompt(self, parser: PromptParser) -> None:
        """Test parsing a complexity evaluation prompt."""
        prompt = """
<ROLE>You are a **PENDING** agent evaluating task complexity.
Decide: **SIMPLE** (become WORKER, execute) or **COMPLEX** (become MANAGER, decompose)</ROLE>

<TASK_TO_EVALUATE>[Builder] Compile project with ASAN</TASK_TO_EVALUATE>

<DECISION_GUIDE>| COMPLEX (→ MANAGER) | SIMPLE (→ WORKER) |
|---------------------|-------------------|
| Multiple domains | Single task |
| Requires coordination | Clear requirements |</DECISION_GUIDE>

<OUTPUT_FORMAT>Return ONLY valid JSON:
```json
{"complexity": "simple" | "complex", "reasoning": "string"}
```</OUTPUT_FORMAT>

<COMPLEXITY_EVALUATION_STRATEGY>## Binary Classification
Classify the task as **SIMPLE** or **COMPLEX**</COMPLEXITY_EVALUATION_STRATEGY>
"""
        sections = parser.parse(prompt)

        tags = {s.tag for s in sections}
        assert "ROLE" in tags
        assert "TASK_TO_EVALUATE" in tags
        assert "DECISION_GUIDE" in tags
        assert "OUTPUT_FORMAT" in tags
        assert "COMPLEXITY_EVALUATION_STRATEGY" in tags

        # All should be TEMPLATE
        for section in sections:
            assert section.provenance == SectionProvenance.TEMPLATE
