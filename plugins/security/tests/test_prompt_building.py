"""Security-plugin prompt building integration tests."""

from pathlib import Path
from uuid import uuid4

from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_parser import PromptParser
from core.domain.aggregates.agent_session import AgentRole
from core.domain.values.limits import HierarchyLimits
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import CVEInstance, SecurityDomainPlugin


def get_prompts_dir() -> Path:
    docker_path = Path("/app/prompts")
    if docker_path.exists():
        return docker_path
    local_path = Path(__file__).parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    return Path("prompts")


PROMPTS_DIR = get_prompts_dir()


def make_test_cve_instance() -> CVEInstance:
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


class TestSecurityPromptBuilding:
    def test_secbench_manager_prompt_strategy_path_renders(self) -> None:
        parser = PromptParser()
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
        assert any(section.tag == "persona" for section in sections)
        assert "<cve_instance>" in prompt
        assert "Builder Manager" in prompt

    def test_secbench_assessment_renders_template_not_inline(self) -> None:
        """Assessment uses domains/secbench/assess.j2, not hardcoded text."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )
        root_id = uuid4()
        limits = HierarchyLimits.create_root(
            root_id=root_id, max_depth=4, max_children_per_node=5, max_retries=2,
        ).for_child()

        briefing = Briefing(
            parent_task="Analyse CVE-2024-0001",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id=str(uuid4()), role="boss", task_summary="[Builder] Build environment"),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="[Builder] Set up the build environment",
            agent_id=uuid4(),
            briefing=briefing,
            hierarchy_limits=limits,
            domain_context=make_test_cve_instance(),
        )

        # Template-rendered content (not the old inline text)
        assert "<domain_operation>" in prompt
        assert "Builder phase scope" in prompt
        # Old inline markers must be gone
        assert "well-scoped leaf task" not in prompt
        assert "Do NOT decompose further" not in prompt

    def test_secbench_assessment_omits_recon_tools(self) -> None:
        """SEC-bench assessment must not include recon tool instructions."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )
        root_id = uuid4()
        limits = HierarchyLimits.create_root(
            root_id=root_id, max_depth=4, max_children_per_node=5, max_retries=2,
        ).for_child()

        briefing = Briefing(
            parent_task="Analyse CVE-2024-0001",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id=str(uuid4()), role="boss", task_summary="[Exploiter] Craft PoC"),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="[Exploiter] Craft a proof of concept",
            agent_id=uuid4(),
            briefing=briefing,
            hierarchy_limits=limits,
            domain_context=make_test_cve_instance(),
        )

        assert "search_codebase" not in prompt
        assert "read_file" not in prompt
        assert "get_symbols_overview" not in prompt

    def test_secbench_assessment_includes_output_schema(self) -> None:
        """Assessment must include the JSON output format."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )
        root_id = uuid4()
        limits = HierarchyLimits.create_root(
            root_id=root_id, max_depth=4, max_children_per_node=5, max_retries=2,
        ).for_child()

        briefing = Briefing(
            parent_task="Analyse CVE-2024-0001",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id=str(uuid4()), role="boss", task_summary="[Fixer] Patch vuln"),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="[Fixer] Generate minimal patch",
            agent_id=uuid4(),
            briefing=briefing,
            hierarchy_limits=limits,
            domain_context=make_test_cve_instance(),
        )

        assert "<output_format>" in prompt
        assert '"action": "execute"' in prompt
        assert '"action": "decompose"' in prompt

    def test_secbench_assessment_depth1_includes_decomposition_guidance(self) -> None:
        """Depth-1 (direct BOSS child) should see decomposition guidance."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )
        root_id = uuid4()
        limits = HierarchyLimits.create_root(
            root_id=root_id, max_depth=4, max_children_per_node=5, max_retries=2,
        ).for_child()

        briefing = Briefing(
            parent_task="Analyse CVE-2024-0001",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id=str(uuid4()), role="boss", task_summary="[Builder] Build environment"),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="[Builder] Set up the build environment",
            agent_id=uuid4(),
            briefing=briefing,
            hierarchy_limits=limits,
            domain_context=make_test_cve_instance(),
        )

        assert "Decision guidance" in prompt
        assert "pre-scoped sub-task" not in prompt

    def test_secbench_assessment_depth2_biases_toward_execute(self) -> None:
        """Depth-2+ (sub-task from manager) should strongly bias toward execute."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )
        root_id = uuid4()
        limits = HierarchyLimits.create_root(
            root_id=root_id, max_depth=4, max_children_per_node=5, max_retries=2,
        ).for_child().for_child()

        briefing = Briefing(
            parent_task="[Builder-3] Improve build script",
            parent_role="manager",
            ancestry=(
                Ancestor(agent_id=str(uuid4()), role="boss", task_summary="[Builder] Build environment"),
                Ancestor(agent_id=str(uuid4()), role="manager", task_summary="[Builder-3] Improve build script"),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="[Builder-3] Improve build script",
            agent_id=uuid4(),
            briefing=briefing,
            hierarchy_limits=limits,
            domain_context=make_test_cve_instance(),
        )

        assert "pre-scoped sub-task" in prompt
        assert "Decision guidance" not in prompt

    def test_secbench_assessment_at_max_depth_forces_execute(self) -> None:
        """When at_max_depth is True, the prompt must force execute."""
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            domain_plugin=SecurityDomainPlugin(),
        )
        root_id = uuid4()
        # max_depth=1, current_depth=1 → at_max_depth
        limits = HierarchyLimits.create_root(
            root_id=root_id, max_depth=1, max_children_per_node=5, max_retries=2,
        ).for_child()

        briefing = Briefing(
            parent_task="Analyse CVE-2024-0001",
            parent_role="boss",
            ancestry=(
                Ancestor(agent_id=str(uuid4()), role="boss", task_summary="[Exploiter] Craft PoC"),
            ),
        )

        prompt = builder.build_assessment_prompt(
            task_description="[Exploiter] Craft a proof of concept",
            agent_id=uuid4(),
            briefing=briefing,
            hierarchy_limits=limits,
            domain_context=make_test_cve_instance(),
        )

        assert "CONSTRAINT: Maximum depth reached" in prompt
        assert "MUST execute directly" in prompt

    def test_secbench_worker_prompt_strategy_path_renders(self) -> None:
        parser = PromptParser(
            extra_tag_mappings=SecurityDomainPlugin().get_tag_mappings(),
            extra_provenance_patterns=SecurityDomainPlugin().get_provenance_patterns(),
        )
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
        assert any(section.tag == "persona" for section in sections)
        assert "<cve_instance>" in prompt
        assert "Exploiter Worker" in prompt
