"""Integration tests for prompt building with real templates.

Tests the PromptBuilder against actual Jinja2 templates and verifies
prompt parsing and provenance classification.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_parser import PromptParser
from core.domain.values.context.sibling_to_sibling import (
    SharedDecision,
    SiblingStatus,
    SiblingView,
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
    local_path = Path(__file__).parent.parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    # Last resort: relative path from CWD
    return Path("prompts")


PROMPTS_DIR = get_prompts_dir()


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

    def test_complexity_evaluation_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that complexity evaluation prompt renders from real templates."""
        prompt = builder.build_complexity_evaluation_prompt(
            task_description="Fix the buffer overflow vulnerability",
            agent_id=uuid4(),
            parent_task="Analyze security issues",
        )

        # Verify prompt is non-empty and contains expected sections
        assert len(prompt) > 100  # Non-trivial content
        sections = parser.parse(prompt)

        # Should have ROLE from pending.j2
        tags = [s.tag for s in sections]
        assert "ROLE" in tags

        # Should have task to evaluate
        assert "TASK_TO_EVALUATE" in tags

        # Should have parent task
        assert "PARENT_TASK" in tags

    def test_boss_delegation_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that boss delegation prompt renders from real templates."""
        builder.set_run_context(user_prompt="Fix the CVE vulnerability")

        prompt = builder.build_boss_delegation_prompt(
            task_description="Fix the CVE vulnerability",
            agent_id=uuid4(),
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Should have ROLE from boss.j2
        assert "ROLE" in tags

        # Should have CAPABILITIES and CONSTRAINTS
        assert "CAPABILITIES" in tags
        assert "CONSTRAINTS" in tags

        # Verify boss role content
        role_section = next(s for s in sections if s.tag == "ROLE")
        assert "BOSS" in role_section.content

    def test_manager_decomposition_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that manager decomposition prompt renders from real templates."""
        builder.set_run_context(user_prompt="Patch the security vulnerability")

        prompt = builder.build_manager_decomposition_prompt(
            task_description="Develop and test the security patch",
            agent_id=uuid4(),
            parent_task="Patch the security vulnerability",
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Should have ROLE from manager.j2
        assert "ROLE" in tags

        # Verify manager role content
        role_section = next(s for s in sections if s.tag == "ROLE")
        assert "MANAGER" in role_section.content

    def test_worker_prompt_uses_real_templates(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that worker prompt renders from real templates."""
        builder.set_run_context(user_prompt="Build the project with ASAN")

        prompt = builder.build_worker_prompt(
            task_description="Compile the project with AddressSanitizer enabled",
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Should have ROLE from worker.j2
        assert "ROLE" in tags

        # Should have TASK
        assert "TASK" in tags

        # Verify worker role content
        role_section = next(s for s in sections if s.tag == "ROLE")
        assert "WORKER" in role_section.content


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

        # These should all be TEMPLATE
        assert "ROLE" in template_tags
        assert "CAPABILITIES" in template_tags
        assert "CONSTRAINTS" in template_tags
        assert "TASK" in template_tags

    def test_parent_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that parent context sections get PARENT provenance."""
        sibling_view = SiblingView(
            current_agent_id=str(uuid4()),
            parent_task="Parent's important task",
            sibling_tasks=(),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="Child task",
            sibling_view=sibling_view,
        )
        sections = parser.parse(prompt)

        parent_sections = [s for s in sections if s.provenance == SectionProvenance.PARENT]
        assert len(parent_sections) >= 1
        assert any(s.tag == "parent-context" for s in parent_sections)

    def test_sibling_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that sibling context sections get SIBLING provenance."""
        sibling_view = SiblingView(
            current_agent_id=str(uuid4()),
            parent_task="Parent task",
            sibling_tasks=(
                SiblingStatus(
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
            sibling_view=sibling_view,
        )
        sections = parser.parse(prompt)

        sibling_sections = [s for s in sections if s.provenance == SectionProvenance.SIBLING]
        assert len(sibling_sections) >= 1
        assert any(s.tag == "sibling-tasks" for s in sibling_sections)

    def test_shared_sections_classified_correctly(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that shared context sections get SHARED provenance."""
        sibling_view = SiblingView(
            current_agent_id=str(uuid4()),
            parent_task="Task",
            sibling_tasks=(),
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
            sibling_view=sibling_view,
        )
        sections = parser.parse(prompt)

        shared_sections = [s for s in sections if s.provenance == SectionProvenance.SHARED]
        assert len(shared_sections) >= 1
        assert any(s.tag == "shared-decisions" for s in shared_sections)

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
