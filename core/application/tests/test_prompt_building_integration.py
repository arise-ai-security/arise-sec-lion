"""Integration tests for prompt building with real templates.

Tests the PromptBuilder against actual Jinja2 templates and verifies
prompt parsing and provenance classification.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_parser import PromptParser
from core.domain.aggregates.agent_session import AgentRole
from core.domain.values.node_message import (
    Ancestor,
    SharedDecision,
    PeerStatus,
    Briefing,
    Handoff,
)
from core.domain.values.prompt_trace import SectionProvenance
from plugins.security import CVEInstance, SecurityDomainPlugin


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


def make_test_cve_instance() -> CVEInstance:
    """Create a minimal SEC-bench CVE instance for prompt tests."""
    return CVEInstance(
        instance_id="demo.cve-2024-0001",
        repo="demo/project",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="Heap overflow in demo parser.",
        base_commit="a" * 40,
    )


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
            task_description="Fix the buffer overflow vulnerability",
            agent_id=uuid4(),
        )

        assert len(prompt) > 100
        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]
        assert "persona" in tags
        assert "operation" in tags
        assert "task" in tags
        assert "output_format" in tags

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
        builder.set_run_context(user_prompt="Patch the security vulnerability")

        prompt = builder.build_manager_decomposition_prompt(
            task_description="Develop and test the security patch",
            agent_id=uuid4(),
            parent_task="Patch the security vulnerability",
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
        builder.set_run_context(user_prompt="Build the project with ASAN")

        prompt = builder.build_worker_prompt(
            task_description="Compile the project with AddressSanitizer enabled",
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

    def test_secbench_manager_prompt_strategy_path_renders(
        self, parser: PromptParser
    ) -> None:
        """SEC-bench manager path should render without stale helper errors."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )

        briefing = Briefing(
            parent_task="Top-level SEC-bench task",
            parent_role="boss",
            ancestry=(
                Ancestor(
                    agent_id=str(uuid4()),
                    role="boss",
                    task_summary="[Builder] Prepare the benchmark environment",
                ),
            ),
        )

        prompt = builder.build_manager_decomposition_prompt(
            task_description="[Builder] Verify the environment setup",
            agent_id=uuid4(),
            agent_role=AgentRole.MANAGER,
            domain_context=make_test_cve_instance(),
            briefing=briefing,
        )

        sections = parser.parse(prompt)
        assert any(s.tag == "persona" for s in sections)
        assert "<cve_instance>" in prompt
        assert "Builder Manager" in prompt

    def test_secbench_worker_prompt_strategy_path_renders(
        self, parser: PromptParser
    ) -> None:
        """SEC-bench worker path should render without stale helper errors."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )

        briefing = Briefing(
            parent_task="Top-level SEC-bench task",
            parent_role="manager",
            ancestry=(
                Ancestor(
                    agent_id=str(uuid4()),
                    role="manager",
                    task_summary="[Exploiter] Develop a proof of concept",
                ),
            ),
        )

        prompt = builder.build_worker_prompt(
            task_description="Reproduce the vulnerability with a PoC",
            domain_context=make_test_cve_instance(),
            briefing=briefing,
        )

        sections = parser.parse(prompt)
        assert any(s.tag == "persona" for s in sections)
        assert "<cve_instance>" in prompt
        assert "Exploiter Worker" in prompt


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
