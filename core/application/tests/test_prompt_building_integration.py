"""Integration tests for prompt building with real templates.

Tests the PromptBuilder against actual Jinja2 templates and verifies
prompt parsing and provenance classification.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services import PromptBuilder, PromptParser
from core.domain.values.node_message import (
    Handoff,
    PeerStatus,
    SharedDecision,
)
from core.domain.values.prompt_capabilities import (
    PromptCapabilities,
    PromptToolDescriptor,
)
from core.domain.values.prompt_trace import SectionProvenance


# Path to actual templates - works both locally and in Docker
# In Docker: /app/prompts, locally: relative to repo root
def get_prompts_dir() -> Path:
    """Get the prompts directory path that works in Docker and locally."""
    # Try Docker path first
    docker_path = Path("/app/prompts")
    if docker_path.exists():
        return docker_path
    # Fall back to relative path from test file
    local_path = Path(__file__).parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    # Last resort: relative path from CWD
    return Path("prompts")


PROMPTS_DIR = get_prompts_dir()


def get_experiments_dir() -> Path:
    """Get the experiments directory path that works in Docker and locally."""
    docker_path = Path("/app/experiments")
    if docker_path.exists():
        return docker_path
    local_path = Path(__file__).parent.parent.parent.parent / "experiments"
    if local_path.exists():
        return local_path
    return Path("experiments")


EXPERIMENTS_DIR = get_experiments_dir()


class TestPromptBuilderWithRealTemplates:
    """Tests that PromptBuilder works with actual templates from prompts/ directory."""

    @pytest.fixture
    def builder(self) -> PromptBuilder:
        """Create a PromptBuilder with real templates."""
        return PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
        )

    @pytest.fixture
    def parser(self) -> PromptParser:
        """Create a PromptParser."""
        return PromptParser()

    def test_assessment_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that assessment prompt renders from real templates."""
        prompt = builder.build_assessment_prompt(
            task_description="Fix the parsing error in the request handler",
            agent_id=uuid4(),
        )

        assert len(prompt) > 100
        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]
        assert "persona" in tags
        assert "operation" in tags
        assert "task" in tags
        assert "output_format" in tags
        assert "**Available tools:**" not in prompt
        assert "Additional tool-calling capabilities are not available for this assessment." in prompt

    def test_assessment_prompt_renders_only_available_recon_tools(
        self, builder: PromptBuilder
    ) -> None:
        """Assessment prompt should only render runtime-available recon tools."""
        capabilities = PromptCapabilities(
            available_tools=(
                PromptToolDescriptor(
                    name="search_codebase",
                    description="Search for a regex pattern across source files.",
                    parameters=("pattern", "path"),
                    required_parameters=("pattern",),
                ),
                PromptToolDescriptor(
                    name="read_symbol",
                    description="Read only the requested symbol body.",
                    parameters=("path", "symbol_name"),
                    required_parameters=("path", "symbol_name"),
                ),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="Find the relevant parser code",
            agent_id=uuid4(),
            prompt_capabilities=capabilities,
        )

        assert "`search_codebase(pattern, path?)`" in prompt
        assert "`read_symbol(path, symbol_name)`" in prompt
        assert "list_directory" not in prompt

    def test_boss_delegation_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that boss delegation prompt renders from real templates."""
        builder.set_run_context(user_prompt="Fix the parser defect")

        prompt = builder.build_boss_delegation_prompt(
            task_description="Fix the parser defect",
            agent_id=uuid4(),
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Should have persona from roles/boss.j2
        assert "persona" in tags

        # Verify boss role content
        persona_section = next(s for s in sections if s.tag == "persona")
        assert "BOSS" in persona_section.content

        # Should have system tier
        assert "system" in tags

    def test_manager_decomposition_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that manager decomposition prompt renders from real templates."""
        builder.set_run_context(user_prompt="Patch the parser defect")

        prompt = builder.build_manager_decomposition_prompt(
            task_description="Develop and test the parser patch",
            agent_id=uuid4(),
            parent_task="Patch the parser defect",
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Should have persona from roles/manager.j2
        assert "persona" in tags

        # Verify manager role content
        persona_section = next(s for s in sections if s.tag == "persona")
        assert "MANAGER" in persona_section.content

    def test_worker_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that worker prompt renders from real templates."""
        builder.set_run_context(user_prompt="Build the project in debug mode")

        prompt = builder.build_worker_prompt(
            task_description="Compile the project and run the smoke checks",
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Should have persona from roles/worker.j2
        assert "persona" in tags

        # Should have task from operations/execution.j2
        assert "task" in tags

        # Verify worker role content
        persona_section = next(s for s in sections if s.tag == "persona")
        assert "WORKER" in persona_section.content
        assert "<config_reference>" not in prompt

class TestProvenanceClassificationWithRealPrompts:
    """Tests that provenance classification works correctly with real prompts."""

    @pytest.fixture
    def builder(self) -> PromptBuilder:
        return PromptBuilder(template_dir=PROMPTS_DIR, default_tool="claude_code")

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_template_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that static template sections get TEMPLATE provenance."""
        prompt = builder.build_worker_prompt(task_description="Test task")
        sections = parser.parse(prompt)

        template_sections = [s for s in sections if s.provenance == SectionProvenance.TEMPLATE]
        template_tags = [s.tag for s in template_sections]

        # These should all be TEMPLATE (4-tier architecture tags)
        assert "system" in template_tags
        assert "persona" in template_tags
        assert "operation" in template_tags
        assert "task" in template_tags

    def test_parent_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that parent context sections get PARENT provenance."""
        handoff = Handoff(
            parent_task="Parent's important task",
            siblings=(),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="Child task",
            handoff=handoff,
        )
        sections = parser.parse(prompt)

        parent_sections = [s for s in sections if s.provenance == SectionProvenance.PARENT]
        assert len(parent_sections) >= 1
        assert any(s.tag == "parent_context" for s in parent_sections)

    def test_sibling_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that sibling context sections get SIBLING provenance."""
        handoff = Handoff(
            parent_task="Parent task",
            siblings=(
                PeerStatus(
                    agent_id=str(uuid4()),
                    sibling_index=0,
                    status="completed",
                    task_summary="Sibling task",
                    result_summary="Done",
                ),
            ),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="My task",
            handoff=handoff,
        )
        sections = parser.parse(prompt)

        sibling_sections = [s for s in sections if s.provenance == SectionProvenance.SIBLING]
        assert len(sibling_sections) >= 1
        assert any(s.tag == "sibling_tasks" for s in sibling_sections)

    def test_sibling_update_instructions_omit_last_worker(
        self, builder: PromptBuilder
    ) -> None:
        """Last worker should not be told to update nonexistent downstream siblings."""
        handoff = Handoff(
            parent_task="Parent task",
            current_sibling_index=1,
            siblings=(
                PeerStatus(
                    agent_id=str(uuid4()),
                    sibling_index=0,
                    status="completed",
                    task_summary="Earlier worker task",
                    result_summary="Done",
                ),
            ),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="My task",
            handoff=handoff,
        )

        assert "<context_update_instructions>" not in prompt

    def test_sibling_update_instructions_render_for_non_last_worker(
        self, builder: PromptBuilder
    ) -> None:
        """Workers with downstream peers should still see the context update instructions."""
        handoff = Handoff(
            parent_task="Parent task",
            current_sibling_index=0,
            siblings=(
                PeerStatus(
                    agent_id=str(uuid4()),
                    sibling_index=1,
                    status="pending",
                    task_summary="Later worker task",
                    result_summary=None,
                ),
            ),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="My task",
            handoff=handoff,
        )

        assert "<context_update_instructions>" in prompt

    def test_shared_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that shared context sections get SHARED provenance."""
        handoff = Handoff(
            parent_task="Task",
            siblings=(),
            shared_decisions=(
                SharedDecision(
                    key="test_key",
                    value="test_value",
                    rationale="Test rationale",
                ),
            ),
        )

        prompt = builder.build_worker_prompt(
            task_description="Task",
            handoff=handoff,
        )
        sections = parser.parse(prompt)

        shared_sections = [s for s in sections if s.provenance == SectionProvenance.SHARED]
        assert len(shared_sections) >= 1
        assert any(s.tag == "shared_decisions" for s in shared_sections)

    def test_system_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that system context sections get SYSTEM provenance."""
        builder.set_run_context(user_prompt="User's original request")

        prompt = builder.build_boss_delegation_prompt(
            task_description="Task",
            agent_id=uuid4(),
        )
        sections = parser.parse(prompt)

        system_sections = [s for s in sections if s.provenance == SectionProvenance.SYSTEM]
        assert len(system_sections) >= 1
        assert any(s.tag == "user_prompt" for s in system_sections)


class TestPromptBuilderMultiTemplateDir:
    """Tests that PromptBuilder accepts single-path and list-of-paths template_dir."""

    def test_single_path_str_keeps_backward_compat(self) -> None:
        """Passing a str template_dir leaves the loader behavior unchanged."""
        # Given: a PromptBuilder constructed with a single string path
        # When: template_dir is set
        builder = PromptBuilder(template_dir=str(PROMPTS_DIR), default_tool="claude_code")

        # Then: template_dir, template_dirs, and the loader reflect the single path
        assert builder.template_dir == PROMPTS_DIR
        assert builder.template_dirs == (PROMPTS_DIR,)
        assert builder.env.get_template("system.j2") is not None

    def test_single_path_object_keeps_backward_compat(self) -> None:
        """Passing a Path template_dir leaves the loader behavior unchanged."""
        # Given: a PromptBuilder constructed with a single Path
        # When: template_dir is set
        builder = PromptBuilder(template_dir=PROMPTS_DIR, default_tool="claude_code")

        # Then: template_dirs collapses to the single path tuple
        assert builder.template_dir == PROMPTS_DIR
        assert builder.template_dirs == (PROMPTS_DIR,)

    def test_list_template_dirs_loads_from_both_roots(self) -> None:
        """A list template_dir lets Jinja find files in either root."""
        # Given: a PromptBuilder that searches prompts/ and experiments/
        # When: the loader is asked for files from each root
        builder = PromptBuilder(
            template_dir=[PROMPTS_DIR, EXPERIMENTS_DIR],
            default_tool="claude_code",
        )

        # Then: template_dirs exposes both roots and each file resolves
        assert builder.template_dirs == (PROMPTS_DIR, EXPERIMENTS_DIR)
        assert builder.template_dir == PROMPTS_DIR
        assert builder.env.get_template("system.j2") is not None
        assert builder.env.get_template("domain_briefing.md") is not None

    def test_empty_list_template_dir_rejected(self) -> None:
        """An empty template_dir list raises early rather than silently no-oping."""
        # Given: an empty list of template paths
        # When: constructing a PromptBuilder
        # Then: a ValueError surfaces immediately
        with pytest.raises(ValueError, match="at least one path"):
            PromptBuilder(template_dir=[], default_tool="claude_code")

    def test_domain_briefing_is_includable_via_jinja(self) -> None:
        """A template can include domain_briefing.md when experiments/ is on the loader path."""
        # Given: a PromptBuilder with prompts/ and experiments/ on the loader path
        builder = PromptBuilder(
            template_dir=[PROMPTS_DIR, EXPERIMENTS_DIR],
            default_tool="claude_code",
        )

        # When: a template string includes the briefing
        rendered = builder.env.from_string(
            "{% include 'domain_briefing.md' %}"
        ).render()

        # Then: canonical briefing content appears in the output
        assert "# SEC-bench Task Context" in rendered
        assert "secb build" in rendered
        assert "## 5. Anti-Cheat Rules" in rendered
