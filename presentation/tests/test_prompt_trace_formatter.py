"""Unit tests for prompt trace formatters.

Tests TreeRenderer, JsonRenderer, and SiblingFlowRenderer.
"""

import json
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
from presentation.formatters.prompt_trace_formatter import (
    JsonRenderer,
    SiblingFlowRenderer,
    TreeRenderer,
    get_renderer,
)


def create_section(
    tag: str,
    content: str,
    provenance: SectionProvenance = SectionProvenance.TEMPLATE,
) -> PromptSection:
    """Helper to create PromptSection."""
    return PromptSection(tag=tag, content=content, provenance=provenance)


def create_prompt(
    sections: tuple[PromptSection, ...] = (),
    prompt_type: str = "task_decomposition",
    target: str = "llm",
) -> ParsedPrompt:
    """Helper to create ParsedPrompt."""
    return ParsedPrompt(
        raw="<raw prompt content>",
        occurred_at=datetime.now(UTC),
        prompt_type=prompt_type,
        target=target,
        sections=sections,
    )


def create_node(
    role: str = "boss",
    depth: int = 0,
    task: str = "Test task",
    sibling_index: int = 0,
    prompts: tuple[ParsedPrompt, ...] = (),
    children: tuple["AgentNode", ...] = (),
) -> AgentNode:
    """Helper to create AgentNode."""
    return AgentNode(
        agent_id=uuid4(),
        role=role,
        depth=depth,
        task=task,
        sibling_index=sibling_index,
        prompts=prompts,
        children=children,
    )


class TestTreeRenderer:
    """Tests for TreeRenderer."""

    @pytest.fixture
    def renderer(self) -> TreeRenderer:
        return TreeRenderer()

    @pytest.fixture
    def default_options(self) -> RenderOptions:
        return RenderOptions()

    def test_render_single_agent(self, renderer, default_options) -> None:
        """Test rendering a single agent without children."""
        node = create_node(role="boss", task="Fix the reported issue")
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)

        assert "PROMPT TRACE:" in output
        assert "1 agents" in output
        assert "max_depth=0" in output
        assert "[BOSS]" in output
        assert "Fix the reported issue" in output

    def test_render_with_prompts(self, renderer, default_options) -> None:
        """Test rendering agent with prompts."""
        sections = (
            create_section("ROLE", "You are a BOSS agent.", SectionProvenance.TEMPLATE),
            create_section("domain_context", "ISSUE-2023-1234", SectionProvenance.SYSTEM),
        )
        prompt = create_prompt(sections)
        node = create_node(prompts=(prompt,))
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)

        assert "BASE TEMPLATE" in output
        assert "SYSTEM" in output
        assert "<ROLE>" in output
        assert "<domain_context>" in output

    def test_render_hierarchy_with_children(self, renderer, default_options) -> None:
        """Test rendering hierarchy with children."""
        worker1 = create_node(role="worker", depth=1, task="Worker 1", sibling_index=0)
        worker2 = create_node(role="worker", depth=1, task="Worker 2", sibling_index=1)
        boss = create_node(role="boss", depth=0, task="Boss task", children=(worker1, worker2))
        trace = HierarchyTrace(root=boss, total_agents=3, max_depth=1)

        output = renderer.render(trace, default_options)

        assert "[BOSS]" in output
        assert "[WORKER]" in output
        assert "SPAWNED: 2 children" in output
        assert "Worker 1" in output
        assert "Worker 2" in output

    def test_render_respects_depth_filter(self, renderer) -> None:
        """Test that depth filter limits output."""
        grandchild = create_node(role="worker", depth=2, task="Grandchild")
        child = create_node(role="manager", depth=1, task="Child", children=(grandchild,))
        boss = create_node(role="boss", depth=0, task="Boss", children=(child,))
        trace = HierarchyTrace(root=boss, total_agents=3, max_depth=2)

        options = RenderOptions(max_depth=1)
        output = renderer.render(trace, options)

        assert "[BOSS]" in output
        assert "[MANAGER]" in output
        assert "Grandchild" not in output  # depth=2 filtered out

    def test_render_respects_role_filter(self, renderer) -> None:
        """Test that role filter shows only matching roles."""
        worker = create_node(role="worker", depth=1, task="Worker task")
        manager = create_node(role="manager", depth=1, task="Manager task")
        boss = create_node(role="boss", depth=0, task="Boss task", children=(worker, manager))
        trace = HierarchyTrace(root=boss, total_agents=3, max_depth=1)

        options = RenderOptions(filter_role="worker")
        output = renderer.render(trace, options)

        assert "[WORKER]" in output
        assert "Worker task" in output
        assert "[MANAGER]" not in output
        assert "[BOSS]" not in output

    def test_render_respects_section_filter(self, renderer) -> None:
        """Test that section filter shows only matching sections."""
        sections = (
            create_section("ROLE", "Boss agent", SectionProvenance.TEMPLATE),
            create_section("parent-context", "Parent info", SectionProvenance.PARENT),
        )
        prompt = create_prompt(sections)
        node = create_node(prompts=(prompt,))
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        options = RenderOptions(section_filter="parent-context")
        output = renderer.render(trace, options)

        assert "<parent-context>" in output
        assert "<ROLE>" not in output

    def test_render_provenance_colors(self, renderer, default_options) -> None:
        """Test that each provenance has distinct indicator."""
        sections = (
            create_section("ROLE", "template", SectionProvenance.TEMPLATE),
            create_section("parent-context", "parent", SectionProvenance.PARENT),
            create_section("sibling-tasks", "sibling", SectionProvenance.SIBLING),
            create_section("decisions", "shared", SectionProvenance.SHARED),
            create_section("domain_context", "system", SectionProvenance.SYSTEM),
        )
        prompt = create_prompt(sections)
        node = create_node(prompts=(prompt,))
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)

        assert "BASE TEMPLATE" in output
        assert "FROM PARENT" in output
        assert "FROM SIBLINGS" in output
        assert "SHARED" in output
        assert "SYSTEM" in output

    def test_render_role_icons(self, renderer, default_options) -> None:
        """Test that roles have correct icons."""
        trace = HierarchyTrace(
            root=create_node(role="boss"),
            total_agents=1,
            max_depth=0,
        )
        output = renderer.render(trace, default_options)
        assert "👔" in output  # Boss icon

    def test_render_sibling_index_shown(self, renderer, default_options) -> None:
        """Test that sibling_index > 0 is shown."""
        worker = create_node(role="worker", depth=1, sibling_index=2)
        boss = create_node(children=(worker,))
        trace = HierarchyTrace(root=boss, total_agents=2, max_depth=1)

        output = renderer.render(trace, default_options)

        assert "sibling_index=2" in output


class TestJsonRenderer:
    """Tests for JsonRenderer."""

    @pytest.fixture
    def renderer(self) -> JsonRenderer:
        return JsonRenderer()

    @pytest.fixture
    def default_options(self) -> RenderOptions:
        return RenderOptions()

    def test_render_valid_json(self, renderer, default_options) -> None:
        """Test that output is valid JSON."""
        node = create_node()
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)

        # Should not raise
        data = json.loads(output)
        assert "root" in data
        assert "total_agents" in data
        assert "max_depth" in data

    def test_render_includes_metadata(self, renderer, default_options) -> None:
        """Test that JSON includes all metadata."""
        sections = (create_section("ROLE", "Boss", SectionProvenance.TEMPLATE),)
        prompt = create_prompt(sections, prompt_type="task_decomposition", target="llm")
        node = create_node(role="boss", task="Test task", prompts=(prompt,))
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)
        data = json.loads(output)

        root = data["root"]
        assert root["role"] == "boss"
        assert root["task"] == "Test task"
        assert root["depth"] == 0
        assert len(root["prompts"]) == 1
        assert root["prompts"][0]["prompt_type"] == "task_decomposition"
        assert root["prompts"][0]["target"] == "llm"

    def test_render_sections_grouped_by_provenance(self, renderer, default_options) -> None:
        """Test that sections are grouped by provenance in JSON."""
        sections = (
            create_section("ROLE", "Boss", SectionProvenance.TEMPLATE),
            create_section("TASK", "Task", SectionProvenance.TEMPLATE),
            create_section("domain_context", "Context data", SectionProvenance.SYSTEM),
        )
        prompt = create_prompt(sections)
        node = create_node(prompts=(prompt,))
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)
        data = json.loads(output)

        prompt_data = data["root"]["prompts"][0]
        assert "template" in prompt_data["sections"]
        assert "system" in prompt_data["sections"]
        assert len(prompt_data["sections"]["template"]) == 2
        assert len(prompt_data["sections"]["system"]) == 1

    def test_render_hierarchy_structure(self, renderer, default_options) -> None:
        """Test that children are included in JSON."""
        worker = create_node(role="worker", depth=1, task="Worker task")
        boss = create_node(role="boss", depth=0, task="Boss task", children=(worker,))
        trace = HierarchyTrace(root=boss, total_agents=2, max_depth=1)

        output = renderer.render(trace, default_options)
        data = json.loads(output)

        assert len(data["root"]["children"]) == 1
        assert data["root"]["children"][0]["role"] == "worker"

    def test_render_respects_depth_filter(self, renderer) -> None:
        """Test that depth filter works in JSON output."""
        grandchild = create_node(role="worker", depth=2, task="Grandchild")
        child = create_node(role="manager", depth=1, children=(grandchild,))
        boss = create_node(role="boss", depth=0, children=(child,))
        trace = HierarchyTrace(root=boss, total_agents=3, max_depth=2)

        options = RenderOptions(max_depth=1)
        output = renderer.render(trace, options)
        data = json.loads(output)

        # Grandchild should be empty dict (filtered)
        assert data["root"]["children"][0]["children"] == [{}]


class TestSiblingFlowRenderer:
    """Tests for SiblingFlowRenderer."""

    @pytest.fixture
    def renderer(self) -> SiblingFlowRenderer:
        return SiblingFlowRenderer()

    @pytest.fixture
    def default_options(self) -> RenderOptions:
        return RenderOptions()

    def test_render_header(self, renderer, default_options) -> None:
        """Test that sibling flow view has header."""
        node = create_node()
        trace = HierarchyTrace(root=node, total_agents=1, max_depth=0)

        output = renderer.render(trace, default_options)

        assert "SIBLING WORKER DATA FLOW" in output

    def test_render_shows_parent_with_workers(self, renderer, default_options) -> None:
        """Test that parents with worker children are shown."""
        sibling_section = create_section(
            "sibling-tasks",
            "You are worker 2 of 3",
            SectionProvenance.SIBLING,
        )
        worker1 = create_node(
            role="worker",
            depth=1,
            task="Worker 1",
            sibling_index=0,
            prompts=(create_prompt(()),),
        )
        worker2 = create_node(
            role="worker",
            depth=1,
            task="Worker 2",
            sibling_index=1,
            prompts=(create_prompt((sibling_section,)),),
        )
        manager = create_node(
            role="manager",
            depth=0,
            task="Manager task",
            children=(worker1, worker2),
        )
        trace = HierarchyTrace(root=manager, total_agents=3, max_depth=1)

        output = renderer.render(trace, default_options)

        assert "Parent MANAGER" in output
        assert "Workers: 2" in output
        assert "Worker #1" in output
        assert "Worker #2" in output

    def test_render_shows_sibling_data_received(self, renderer, default_options) -> None:
        """Test that received sibling data is shown."""
        sibling_section = create_section(
            "sibling-tasks",
            "You are worker 2 of 3. 1 completed.",
            SectionProvenance.SIBLING,
        )
        worker = create_node(
            role="worker",
            depth=1,
            sibling_index=1,
            prompts=(create_prompt((sibling_section,)),),
        )
        manager = create_node(role="manager", children=(worker,))
        trace = HierarchyTrace(root=manager, total_agents=2, max_depth=1)

        output = renderer.render(trace, default_options)

        assert "Received sibling data" in output
        assert "1 completed" in output

    def test_render_first_worker_no_sibling_data(self, renderer, default_options) -> None:
        """Test that first worker shows no sibling data message."""
        worker = create_node(
            role="worker",
            depth=1,
            sibling_index=0,
            prompts=(create_prompt(()),),  # No sibling section
        )
        manager = create_node(role="manager", children=(worker,))
        trace = HierarchyTrace(root=manager, total_agents=2, max_depth=1)

        output = renderer.render(trace, default_options)

        assert "No sibling data" in output


class TestGetRenderer:
    """Tests for get_renderer factory function."""

    def test_get_tree_renderer(self) -> None:
        """Test getting tree renderer."""
        renderer = get_renderer("tree")
        assert isinstance(renderer, TreeRenderer)

    def test_get_json_renderer(self) -> None:
        """Test getting JSON renderer."""
        renderer = get_renderer("json")
        assert isinstance(renderer, JsonRenderer)

    def test_get_siblings_renderer(self) -> None:
        """Test getting siblings renderer."""
        renderer = get_renderer("siblings")
        assert isinstance(renderer, SiblingFlowRenderer)

    def test_get_unknown_defaults_to_tree(self) -> None:
        """Test that unknown format defaults to tree."""
        renderer = get_renderer("unknown_format")
        assert isinstance(renderer, TreeRenderer)
