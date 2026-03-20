"""Unit tests for prompt trace domain values.

Tests SectionProvenance, PromptSection, ParsedPrompt, AgentNode, HierarchyTrace.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from core.domain.values.prompt_trace import (
    AgentNode,
    HierarchyTrace,
    ParsedPrompt,
    PromptSection,
    RenderOptions,
    SectionProvenance,
)


class TestSectionProvenance:
    """Tests for SectionProvenance enum."""

    def test_all_provenances_exist(self) -> None:
        """Test that all expected provenance types exist."""
        expected = {"template", "parent", "sibling", "children", "shared", "system"}
        actual = {p.value for p in SectionProvenance}
        assert actual == expected

    def test_provenance_is_string_enum(self) -> None:
        """Test that provenance values are strings."""
        for prov in SectionProvenance:
            assert isinstance(prov.value, str)


class TestPromptSection:
    """Tests for PromptSection value object."""

    def test_create_prompt_section(self) -> None:
        """Test creating a PromptSection."""
        section = PromptSection(
            tag="ROLE",
            content="You are a BOSS agent.",
            provenance=SectionProvenance.TEMPLATE,
        )
        assert section.tag == "ROLE"
        assert section.content == "You are a BOSS agent."
        assert section.provenance == SectionProvenance.TEMPLATE

    def test_prompt_section_is_frozen(self) -> None:
        """Test that PromptSection is immutable."""
        section = PromptSection(
            tag="ROLE",
            content="content",
            provenance=SectionProvenance.TEMPLATE,
        )
        with pytest.raises(AttributeError):
            section.tag = "NEW_TAG"

    def test_prompt_section_equality(self) -> None:
        """Test that PromptSections with same values are equal."""
        section1 = PromptSection("ROLE", "content", SectionProvenance.TEMPLATE)
        section2 = PromptSection("ROLE", "content", SectionProvenance.TEMPLATE)
        assert section1 == section2

    def test_prompt_section_hash(self) -> None:
        """Test that PromptSections can be used in sets."""
        section1 = PromptSection("ROLE", "content", SectionProvenance.TEMPLATE)
        section2 = PromptSection("ROLE", "content", SectionProvenance.TEMPLATE)
        section3 = PromptSection("TASK", "other", SectionProvenance.TEMPLATE)

        section_set = {section1, section2, section3}
        assert len(section_set) == 2  # section1 and section2 are equal


class TestParsedPrompt:
    """Tests for ParsedPrompt value object."""

    @pytest.fixture
    def sample_sections(self) -> tuple[PromptSection, ...]:
        return (
            PromptSection("ROLE", "Boss agent", SectionProvenance.TEMPLATE),
            PromptSection("TASK", "Do work", SectionProvenance.TEMPLATE),
            PromptSection("parent-context", "Parent info", SectionProvenance.PARENT),
            PromptSection("work_dir", "/workspace/app", SectionProvenance.SYSTEM),
        )

    def test_create_parsed_prompt(self, sample_sections) -> None:
        """Test creating a ParsedPrompt."""
        now = datetime.now(UTC)
        prompt = ParsedPrompt(
            raw="<ROLE>Boss agent</ROLE>...",
            occurred_at=now,
            prompt_type="task_decomposition",
            target="llm",
            sections=sample_sections,
        )
        assert prompt.raw == "<ROLE>Boss agent</ROLE>..."
        assert prompt.occurred_at == now
        assert prompt.prompt_type == "task_decomposition"
        assert prompt.target == "llm"
        assert len(prompt.sections) == 4

    def test_by_provenance_filters_correctly(self, sample_sections) -> None:
        """Test filtering sections by provenance."""
        prompt = ParsedPrompt(
            raw="...",
            occurred_at=datetime.now(UTC),
            prompt_type="test",
            target="llm",
            sections=sample_sections,
        )

        template_sections = prompt.by_provenance(SectionProvenance.TEMPLATE)
        assert len(template_sections) == 2
        assert all(s.provenance == SectionProvenance.TEMPLATE for s in template_sections)

        parent_sections = prompt.by_provenance(SectionProvenance.PARENT)
        assert len(parent_sections) == 1
        assert parent_sections[0].tag == "parent-context"

        system_sections = prompt.by_provenance(SectionProvenance.SYSTEM)
        assert len(system_sections) == 1

        # No sibling sections
        sibling_sections = prompt.by_provenance(SectionProvenance.SIBLING)
        assert len(sibling_sections) == 0

    def test_raw_length_property(self) -> None:
        """Test raw_length property returns correct length."""
        raw_text = "<ROLE>content</ROLE>"
        prompt = ParsedPrompt(
            raw=raw_text,
            occurred_at=datetime.now(UTC),
            prompt_type="test",
            target="llm",
            sections=(),
        )
        assert prompt.raw_length == len(raw_text)

    def test_parsed_prompt_is_frozen(self) -> None:
        """Test that ParsedPrompt is immutable."""
        prompt = ParsedPrompt(
            raw="...",
            occurred_at=datetime.now(UTC),
            prompt_type="test",
            target="llm",
            sections=(),
        )
        with pytest.raises(AttributeError):
            prompt.raw = "new content"


class TestAgentNode:
    """Tests for AgentNode value object."""

    def test_create_agent_node(self) -> None:
        """Test creating an AgentNode."""
        agent_id = uuid4()
        node = AgentNode(
            agent_id=agent_id,
            role="boss",
            depth=0,
            task="Fix the parser defect",
            sibling_index=0,
            prompts=(),
            children=(),
        )
        assert node.agent_id == agent_id
        assert node.role == "boss"
        assert node.depth == 0
        assert node.task == "Fix the parser defect"
        assert node.sibling_index == 0

    def test_agent_node_with_children(self) -> None:
        """Test creating an AgentNode with children."""
        parent_id = uuid4()
        child1 = AgentNode(uuid4(), "worker", 1, "Task 1", 0, (), ())
        child2 = AgentNode(uuid4(), "worker", 1, "Task 2", 1, (), ())

        parent = AgentNode(
            agent_id=parent_id,
            role="boss",
            depth=0,
            task="Parent task",
            sibling_index=0,
            prompts=(),
            children=(child1, child2),
        )
        assert len(parent.children) == 2
        assert parent.children[0].sibling_index == 0
        assert parent.children[1].sibling_index == 1

    def test_prompt_count_property(self) -> None:
        """Test prompt_count property."""
        prompt1 = ParsedPrompt("...", datetime.now(UTC), "test", "llm", ())
        prompt2 = ParsedPrompt("...", datetime.now(UTC), "test", "llm", ())

        node = AgentNode(
            agent_id=uuid4(),
            role="worker",
            depth=1,
            task="Task",
            sibling_index=0,
            prompts=(prompt1, prompt2),
            children=(),
        )
        assert node.prompt_count == 2

    def test_agent_node_default_children(self) -> None:
        """Test that children defaults to empty tuple."""
        node = AgentNode(
            agent_id=uuid4(),
            role="worker",
            depth=1,
            task="Task",
            sibling_index=0,
            prompts=(),
        )
        assert node.children == ()


class TestHierarchyTrace:
    """Tests for HierarchyTrace value object."""

    def test_create_hierarchy_trace(self) -> None:
        """Test creating a HierarchyTrace."""
        root = AgentNode(uuid4(), "boss", 0, "Root task", 0, (), ())
        trace = HierarchyTrace(
            root=root,
            total_agents=1,
            max_depth=0,
        )
        assert trace.root == root
        assert trace.total_agents == 1
        assert trace.max_depth == 0

    def test_hierarchy_trace_with_tree(self) -> None:
        """Test HierarchyTrace with a multi-level tree."""
        grandchild = AgentNode(uuid4(), "worker", 2, "Grandchild", 0, (), ())
        child = AgentNode(uuid4(), "manager", 1, "Child", 0, (), (grandchild,))
        root = AgentNode(uuid4(), "boss", 0, "Root", 0, (), (child,))

        trace = HierarchyTrace(root=root, total_agents=3, max_depth=2)

        assert trace.total_agents == 3
        assert trace.max_depth == 2
        assert len(trace.root.children) == 1
        assert len(trace.root.children[0].children) == 1

    def test_hierarchy_trace_is_frozen(self) -> None:
        """Test that HierarchyTrace is immutable."""
        root = AgentNode(uuid4(), "boss", 0, "Task", 0, (), ())
        trace = HierarchyTrace(root=root, total_agents=1, max_depth=0)

        with pytest.raises(AttributeError):
            trace.total_agents = 10


class TestRenderOptions:
    """Tests for RenderOptions value object."""

    def test_default_render_options(self) -> None:
        """Test default RenderOptions values."""
        options = RenderOptions()
        assert options.max_depth is None
        assert options.filter_role is None
        assert options.section_filter is None

    def test_custom_render_options(self) -> None:
        """Test creating RenderOptions with custom values."""
        options = RenderOptions(
            max_depth=2,
            filter_role="worker",
            section_filter="parent-context",
        )
        assert options.max_depth == 2
        assert options.filter_role == "worker"
        assert options.section_filter == "parent-context"

    def test_render_options_is_frozen(self) -> None:
        """Test that RenderOptions is immutable."""
        options = RenderOptions()
        with pytest.raises(AttributeError):
            options.max_depth = 5
