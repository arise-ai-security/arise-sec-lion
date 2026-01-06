"""Integration tests for prompt building and context passing system.

Tests the full pipeline from context composition through prompt building
to prompt parsing, validating that all context passing mechanisms work correctly.

Context Passing Types Tested:
- Parent → Child: SpawnPayload, ParentSummary, AncestryChain
- Child → Parent: ChildOutcomes
- Sibling ↔ Sibling: SiblingView, SiblingResults
- Global Context: SharedDecisions, SharedArtifacts
"""

from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_parser import PromptParser
from core.domain.values.context.data_types import (
    AncestorEntry,
    AncestryChain,
    ArtifactEntry,
    ChildOutcomeEntry,
    ChildOutcomes,
    DecisionEntry,
    ParentSummary,
    SharedArtifacts,
    SharedDecisions,
    SiblingEntry,
    SiblingResults,
)
from core.domain.values.context.parent_to_child import AncestorSummary, SpawnPayload
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


class TestParentToChildContextPassing:
    """Tests for parent → child context passing mechanisms."""

    @pytest.fixture
    def builder(self) -> PromptBuilder:
        return PromptBuilder(template_dir=PROMPTS_DIR, default_tool="claude_code")

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_sibling_view_renders_parent_context(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test that SiblingView.parent_task renders in worker prompt."""
        sibling_view = SiblingView(
            current_agent_id=str(uuid4()),
            parent_task="Analyze and fix the SQL injection vulnerability",
            sibling_tasks=(),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="Write the input validation code",
            sibling_view=sibling_view,
        )

        sections = parser.parse(prompt)

        # Should have parent-context from sibling.j2
        parent_ctx = next((s for s in sections if s.tag == "parent-context"), None)
        assert parent_ctx is not None
        assert "SQL injection" in parent_ctx.content
        assert parent_ctx.provenance == SectionProvenance.PARENT

    def test_spawn_payload_ancestry_in_context(self) -> None:
        """Test SpawnPayload carries full ancestry chain."""
        # Build ancestry
        boss_summary = AncestorSummary(
            agent_id=str(uuid4()),
            role="boss",
            task_summary="Fix all security vulnerabilities",
        )
        manager_summary = AncestorSummary(
            agent_id=str(uuid4()),
            role="manager",
            task_summary="Handle authentication bugs",
        )

        payload = SpawnPayload(
            parent_task="Handle authentication bugs",
            parent_role="manager",
            depth=2,
            ancestry=(boss_summary, manager_summary),
            decisions=("use_parameterized_queries", "validate_all_inputs"),
            constraints={},
            execution_limits={"agents_remaining": 5},
        )

        # Verify ancestry is preserved
        assert len(payload.ancestry) == 2
        assert payload.ancestry[0].role == "boss"
        assert payload.ancestry[1].role == "manager"
        assert payload.depth == 2

        # Verify decisions are passed
        assert "use_parameterized_queries" in payload.decisions
        assert "validate_all_inputs" in payload.decisions

    def test_parent_summary_context_renders(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test ParentSummary renders correctly via template."""
        parent_summary = ParentSummary(
            task="Analyze buffer overflow in parse_input()",
            result="Found use-after-free at line 42",
            decisions=("use_smart_pointers", "add_bounds_checking"),
            role="manager",
        )

        # Verify data structure
        template_dict = parent_summary.to_template_dict()
        assert template_dict["task"] == "Analyze buffer overflow in parse_input()"
        assert template_dict["result"] == "Found use-after-free at line 42"
        assert "use_smart_pointers" in template_dict["decisions"]
        assert parent_summary.template_key == "parent_summary"

    def test_ancestry_chain_context_renders(self) -> None:
        """Test AncestryChain renders correctly via template."""
        ancestry = AncestryChain(
            ancestors=(
                AncestorEntry(
                    agent_id=str(uuid4()),
                    role="boss",
                    task_summary="Fix CVE-2023-1234",
                    depth=0,
                ),
                AncestorEntry(
                    agent_id=str(uuid4()),
                    role="manager",
                    task_summary="Develop security patch",
                    depth=1,
                ),
            )
        )

        template_dict = ancestry.to_template_dict()
        assert template_dict["depth"] == 2
        assert len(template_dict["ancestors"]) == 2
        assert template_dict["ancestors"][0]["role"] == "boss"
        assert ancestry.template_key == "ancestry_chain"


class TestChildToParentContextPassing:
    """Tests for child → parent context passing mechanisms."""

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_child_outcomes_structure(self) -> None:
        """Test ChildOutcomes data structure for parent consumption."""
        outcomes = ChildOutcomes(
            outcomes=(
                ChildOutcomeEntry(
                    child_id=str(uuid4()),
                    task_summary="Build project with ASAN",
                    result_text="Build succeeded. Binary at /build/target",
                    artifacts=("target_binary", "build_log"),
                    decisions=("use_asan_flags",),
                    status="completed",
                ),
                ChildOutcomeEntry(
                    child_id=str(uuid4()),
                    task_summary="Run exploit PoC",
                    result_text="Exploit triggered crash at 0xdeadbeef",
                    artifacts=("crash_log",),
                    decisions=(),
                    status="completed",
                ),
            )
        )

        template_dict = outcomes.to_template_dict()
        assert template_dict["total_count"] == 2
        assert template_dict["completed_count"] == 2
        assert template_dict["failed_count"] == 0
        assert len(template_dict["outcomes"]) == 2
        assert outcomes.template_key == "child_outcomes"

    def test_child_outcomes_with_failures(self) -> None:
        """Test ChildOutcomes handles failed children."""
        outcomes = ChildOutcomes(
            outcomes=(
                ChildOutcomeEntry(
                    child_id=str(uuid4()),
                    task_summary="Apply patch",
                    result_text="Patch applied successfully",
                    status="completed",
                ),
                ChildOutcomeEntry(
                    child_id=str(uuid4()),
                    task_summary="Run regression tests",
                    result_text="Tests failed: 3 assertions broken",
                    status="failed",
                ),
            )
        )

        template_dict = outcomes.to_template_dict()
        assert template_dict["total_count"] == 2
        assert template_dict["completed_count"] == 1
        assert template_dict["failed_count"] == 1


class TestSiblingToSiblingContextPassing:
    """Tests for sibling ↔ sibling context passing mechanisms."""

    @pytest.fixture
    def builder(self) -> PromptBuilder:
        return PromptBuilder(template_dir=PROMPTS_DIR, default_tool="claude_code")

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_sibling_view_renders_in_worker_prompt(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test complete SiblingView renders in worker prompt."""
        current_id = str(uuid4())
        sibling_view = SiblingView(
            current_agent_id=current_id,
            parent_task="Fix the authentication vulnerability",
            sibling_tasks=(
                SiblingStatus(
                    agent_id=str(uuid4()),
                    sibling_index=0,
                    status="completed",
                    task_summary="Set up test environment",
                    result_summary="Docker container running on localhost:8080",
                ),
                SiblingStatus(
                    agent_id=current_id,
                    sibling_index=1,
                    status="in_progress",
                    task_summary="Run exploit PoC",
                ),
                SiblingStatus(
                    agent_id=str(uuid4()),
                    sibling_index=2,
                    status="pending",
                    task_summary="Verify fix",
                ),
            ),
            shared_decisions=(
                SharedDecision(
                    key="target_port",
                    value="8080",
                    rationale="Standard HTTP port for testing",
                    decided_by="worker-0",
                ),
            ),
        )

        prompt = builder.build_worker_prompt(
            task_description="Execute the proof-of-concept exploit",
            sibling_view=sibling_view,
        )

        sections = parser.parse(prompt)

        # Should have sibling-tasks section
        sibling_section = next((s for s in sections if s.tag == "sibling-tasks"), None)
        assert sibling_section is not None
        assert sibling_section.provenance == SectionProvenance.SIBLING

        # Should contain sibling info
        assert "3 sibling worker" in sibling_section.content
        assert "1 completed" in sibling_section.content
        assert "1 in progress" in sibling_section.content

        # Should contain completed sibling's result
        assert "Docker container" in sibling_section.content

        # Should have shared-decisions section
        decisions_section = next(
            (s for s in sections if s.tag == "shared-decisions"), None
        )
        assert decisions_section is not None
        assert "target_port" in decisions_section.content
        assert "8080" in decisions_section.content
        assert decisions_section.provenance == SectionProvenance.SHARED

    def test_sibling_results_data_type(self) -> None:
        """Test SiblingResults data structure for prompt composition."""
        siblings = SiblingResults(
            siblings=(
                SiblingEntry(
                    agent_id=str(uuid4()),
                    index=0,
                    status="completed",
                    task_summary="Build with ASAN",
                    result_summary="Build succeeded",
                ),
                SiblingEntry(
                    agent_id=str(uuid4()),
                    index=1,
                    status="in_progress",
                    task_summary="Run fuzzer",
                ),
                SiblingEntry(
                    agent_id=str(uuid4()),
                    index=2,
                    status="pending",
                    task_summary="Analyze crashes",
                ),
            )
        )

        template_dict = siblings.to_template_dict()
        assert template_dict["total_count"] == 3
        assert template_dict["completed_count"] == 1
        assert template_dict["in_progress_count"] == 1
        assert siblings.template_key == "sibling_results"

    def test_first_worker_has_no_sibling_data(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test first worker (sibling_index=0) has no prior sibling data."""
        current_id = str(uuid4())
        sibling_view = SiblingView(
            current_agent_id=current_id,
            parent_task="Fix the bug",
            sibling_tasks=(
                SiblingStatus(
                    agent_id=current_id,
                    sibling_index=0,
                    status="in_progress",
                    task_summary="First task",
                ),
            ),
            shared_decisions=(),
        )

        prompt = builder.build_worker_prompt(
            task_description="Execute first task",
            sibling_view=sibling_view,
        )

        sections = parser.parse(prompt)

        # First worker should not see its own task in sibling-tasks
        # (filtered by template: if sibling.agent_id != current_agent_id)
        sibling_section = next((s for s in sections if s.tag == "sibling-tasks"), None)

        # Template should still render the container but with minimal content
        # since current worker is filtered out
        if sibling_section:
            assert "1 sibling worker" in sibling_section.content


class TestGlobalSharedContext:
    """Tests for global shared context (SharedDecisions, SharedArtifacts)."""

    def test_shared_decisions_structure(self) -> None:
        """Test SharedDecisions for global context sharing."""
        decisions = SharedDecisions(
            decisions=(
                DecisionEntry(
                    key="exploit_type",
                    value="heap_overflow",
                    rationale="Initial analysis shows heap corruption",
                    decided_by="boss-agent",
                ),
                DecisionEntry(
                    key="patch_strategy",
                    value="bounds_checking",
                    rationale="Add size validation before memcpy",
                    decided_by="manager-fixer",
                ),
            )
        )

        template_dict = decisions.to_template_dict()
        assert len(template_dict["decisions"]) == 2
        assert template_dict["decisions"][0]["key"] == "exploit_type"
        assert decisions.template_key == "shared_decisions"

    def test_shared_artifacts_structure(self) -> None:
        """Test SharedArtifacts for global artifact sharing."""
        artifacts = SharedArtifacts(
            artifacts=(
                ArtifactEntry(
                    key="poc_exploit",
                    content_type="text/python",
                    content="#!/usr/bin/env python3\n# PoC exploit code...",
                    stored_by="worker-exploiter",
                ),
                ArtifactEntry(
                    key="crash_analysis",
                    content_type="text/plain",
                    content="ASAN output: heap-buffer-overflow on address 0x...",
                    stored_by="worker-analyzer",
                ),
            )
        )

        template_dict = artifacts.to_template_dict()
        assert len(template_dict["artifacts"]) == 2
        assert template_dict["artifacts"][0]["key"] == "poc_exploit"
        assert artifacts.template_key == "shared_artifacts"

    def test_shared_decisions_parsed_correctly(self) -> None:
        """Test that SharedDecisions rendered in prompts are parsed correctly."""
        parser = PromptParser()

        # Simulate what the decisions.j2 template produces
        rendered = """
<SHARED_DECISIONS>
Key decisions made during this execution (follow these for consistency):

<decision key="target_framework">
<value>django</value>
<rationale>Target application uses Django 3.2</rationale>
</decision>
<decision key="exploit_method">
<value>sql_injection</value>
<rationale>Login endpoint vulnerable to SQLi</rationale>
</decision>
</SHARED_DECISIONS>
"""
        sections = parser.parse(rendered)

        assert len(sections) == 1
        assert sections[0].tag == "SHARED_DECISIONS"
        assert sections[0].provenance == SectionProvenance.SHARED
        assert "target_framework" in sections[0].content
        assert "django" in sections[0].content


class TestFullPromptBuildAndParseCycle:
    """End-to-end tests that build prompts and parse them back."""

    @pytest.fixture
    def builder(self) -> PromptBuilder:
        return PromptBuilder(template_dir=PROMPTS_DIR, default_tool="claude_code")

    @pytest.fixture
    def parser(self) -> PromptParser:
        return PromptParser()

    def test_worker_prompt_full_cycle_with_all_context(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test building a worker prompt with all context types and parsing it."""
        builder.set_run_context(user_prompt="Fix CVE-2023-1234")

        current_id = str(uuid4())
        sibling_view = SiblingView(
            current_agent_id=current_id,
            parent_task="Develop and test the security patch",
            sibling_tasks=(
                SiblingStatus(
                    agent_id=str(uuid4()),
                    sibling_index=0,
                    status="completed",
                    task_summary="Build project with sanitizers",
                    result_summary="Build successful. Binary ready.",
                ),
                SiblingStatus(
                    agent_id=current_id,
                    sibling_index=1,
                    status="in_progress",
                    task_summary="Apply the security patch",
                ),
            ),
            shared_decisions=(
                SharedDecision(
                    key="patch_location",
                    value="src/parser.c:142",
                    rationale="Vulnerable memcpy at this location",
                    decided_by="analyzer-agent",
                ),
            ),
        )

        prompt = builder.build_worker_prompt(
            task_description="Apply bounds checking fix to the vulnerable function",
            sibling_view=sibling_view,
            workspace_context="src/parser.c\nsrc/parser.h\ntests/test_parser.c",
        )

        # Parse the generated prompt
        sections = parser.parse(prompt)
        provenance_map = {s.tag: s.provenance for s in sections}

        # Verify all expected sections are present with correct provenance
        assert "ROLE" in provenance_map
        assert provenance_map["ROLE"] == SectionProvenance.TEMPLATE

        assert "TASK" in provenance_map
        assert provenance_map["TASK"] == SectionProvenance.TEMPLATE

        assert "parent-context" in provenance_map
        assert provenance_map["parent-context"] == SectionProvenance.PARENT

        assert "sibling-tasks" in provenance_map
        assert provenance_map["sibling-tasks"] == SectionProvenance.SIBLING

        assert "shared-decisions" in provenance_map
        assert provenance_map["shared-decisions"] == SectionProvenance.SHARED

        assert "workspace-context" in provenance_map
        assert provenance_map["workspace-context"] == SectionProvenance.SYSTEM

        # Verify content is present
        sibling_section = next(s for s in sections if s.tag == "sibling-tasks")
        assert "Build successful" in sibling_section.content

        decisions_section = next(s for s in sections if s.tag == "shared-decisions")
        assert "patch_location" in decisions_section.content

    def test_boss_prompt_full_cycle(
        self, builder: PromptBuilder, parser: PromptParser
    ) -> None:
        """Test building a boss prompt and parsing it."""
        builder.set_run_context(user_prompt="Fix the critical security vulnerability")

        prompt = builder.build_boss_delegation_prompt(
            task_description="Analyze and fix CVE-2023-1234",
            agent_id=uuid4(),
        )

        sections = parser.parse(prompt)
        tags = [s.tag for s in sections]

        # Boss prompt should have role-defining sections
        assert "ROLE" in tags
        assert "CAPABILITIES" in tags
        assert "CONSTRAINTS" in tags

        # Should have user_prompt section
        assert "user_prompt" in tags

        # Verify provenance classification
        role_section = next(s for s in sections if s.tag == "ROLE")
        assert role_section.provenance == SectionProvenance.TEMPLATE
        assert "BOSS" in role_section.content

        user_section = next(s for s in sections if s.tag == "user_prompt")
        assert user_section.provenance == SectionProvenance.SYSTEM

    def test_all_templates_render_without_error(self, builder: PromptBuilder) -> None:
        """Test that all main prompt types render without template errors."""
        builder.set_run_context(user_prompt="Test task")
        agent_id = uuid4()

        # Each should render without raising TemplateNotFound
        complexity_prompt = builder.build_complexity_evaluation_prompt(
            task_description="Test task",
            agent_id=agent_id,
        )
        assert len(complexity_prompt) > 0

        boss_prompt = builder.build_boss_delegation_prompt(
            task_description="Test task",
            agent_id=agent_id,
        )
        assert len(boss_prompt) > 0

        manager_prompt = builder.build_manager_decomposition_prompt(
            task_description="Test task",
            agent_id=agent_id,
        )
        assert len(manager_prompt) > 0

        worker_prompt = builder.build_worker_prompt(
            task_description="Test task",
        )
        assert len(worker_prompt) > 0


class TestContextDataTemplateKeys:
    """Tests that all context data types have correct template keys."""

    def test_all_context_types_have_template_keys(self) -> None:
        """Verify all context data types implement template_key correctly."""
        # Parent-to-child
        parent = ParentSummary(task="test", role="manager")
        assert parent.template_key == "parent_summary"

        ancestry = AncestryChain(ancestors=())
        assert ancestry.template_key == "ancestry_chain"

        # Sibling
        siblings = SiblingResults(siblings=())
        assert siblings.template_key == "sibling_results"

        # Shared
        decisions = SharedDecisions(decisions=())
        assert decisions.template_key == "shared_decisions"

        artifacts = SharedArtifacts(artifacts=())
        assert artifacts.template_key == "shared_artifacts"

        # Child-to-parent
        outcomes = ChildOutcomes(outcomes=())
        assert outcomes.template_key == "child_outcomes"


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
