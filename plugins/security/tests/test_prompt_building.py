"""Security-plugin prompt building integration tests."""

from pathlib import Path
from uuid import uuid4

from core.application.services.prompt_builder import PromptBuilder
from core.application.services.prompt_parser import PromptParser
from core.domain.aggregates.agent_session import AgentRole
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
