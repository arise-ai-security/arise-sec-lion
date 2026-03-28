"""Security-plugin prompt building integration tests."""

from pathlib import Path
from uuid import uuid4

from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_parser import PromptParser
from core.domain.aggregates.agent_session import AgentRole
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import CVEInstance, SecBenchPromptStrategy, SecurityDomainPlugin


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
    def test_secbench_assessment_prompt_includes_cve_context_without_recon_tools(self) -> None:
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        prompt = builder.build_assessment_prompt(
            task_description="Decide whether to execute or decompose this SEC-bench task",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
        )

        assert "<cve_instance>" in prompt
        assert "demo.cve-2024-0001" in prompt
        assert "**Available tools:**" not in prompt
        assert "Additional tool-calling capabilities are not available for this assessment." in prompt

    def test_secbench_manager_prompt_strategy_path_renders(self) -> None:
        parser = PromptParser()
        domain_plugin = SecurityDomainPlugin()
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
            domain_plugin=domain_plugin,
        )
        builder.set_run_context(user_prompt="Root SEC-bench request")

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
        assert "<user_prompt>\nRoot SEC-bench request\n</user_prompt>" not in prompt
        assert "[Builder] Verify the environment setup" in prompt
        assert "[Builder-1]" not in prompt

    def test_secbench_worker_prompt_strategy_path_renders(self) -> None:
        domain_plugin = SecurityDomainPlugin()
        parser = PromptParser(
            extra_tag_mappings=domain_plugin.get_tag_mappings(),
            extra_provenance_patterns=domain_plugin.get_provenance_patterns(),
        )
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
            domain_plugin=domain_plugin,
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

    def test_secbench_worker_prompt_falls_back_to_task_description_for_phase(self) -> None:
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        prompt = builder.build_worker_prompt(
            task_description="[Fixer] Produce the minimal patch",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        assert "<cve_instance>" in prompt
        assert "Fixer Worker" in prompt
