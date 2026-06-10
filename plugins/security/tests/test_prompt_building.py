"""Security-plugin prompt building integration tests."""

from pathlib import Path
from uuid import uuid4

from core.application.services import PromptBuilder, PromptParser
from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE
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
        assert (
            "Additional tool-calling capabilities are not available for this assessment." in prompt
        )

    def test_secbench_manager_prompt_strategy_path_renders(self) -> None:
        parser = PromptParser()
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
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

    def test_secbench_reporter_prompt_uses_canonical_testcase_alias(self) -> None:
        # Given: a SEC-bench worker prompt for the reporter phase.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="openhands",
            strategy=SecBenchPromptStrategy(),
        )

        # When: building the reporter worker prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Reporter] Write the final security report",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: the prompt directs the file editor to the canonical alias.
        assert "`/testcase/security_report.md`" in prompt
        assert "Do NOT use `/testcase/security_report.md`" not in prompt
        assert "host testcase path" not in prompt


class TestFlatPromptBuilding:
    """Flat-mode (A-cell) control-baseline prompt composition.

    Flat-mode prompts intentionally do NOT include the tree topology's
    role/operation/worker templates — those are part of our system's
    contribution under test. See ``SecBenchPromptStrategy.extend_flat_prompt``.
    """

    def _builder(self) -> PromptBuilder:
        return PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

    def test_build_flat_prompt_renders_cve_and_pipeline_only(self) -> None:
        # Given: a flat-mode PromptBuilder + the SEC-bench strategy.
        builder = self._builder()

        # When: building the flat prompt from a raw dataset slug + CVE context.
        prompt = builder.build_flat_prompt(
            task_description="openjpeg.cve-2016-7445",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
            subagent_enabled=False,
        )

        # Then: the CVE context and the 4-phase pipeline overview are present.
        assert "<cve_instance>" in prompt
        assert "## Vulnerability Reproduction — 4-Phase Process" in prompt
        assert "demo.cve-2024-0001" in prompt
        assert "<task>\nopenjpeg.cve-2016-7445\n</task>" in prompt

    def test_build_flat_prompt_excludes_tree_topology_templates(self) -> None:
        # Given: a flat-mode PromptBuilder.
        builder = self._builder()

        # When: building the flat prompt.
        prompt = builder.build_flat_prompt(
            task_description="openjpeg.cve-2016-7445",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
            subagent_enabled=False,
        )

        # Then: none of our tree-tier templates leak into the flat prompt.
        # NOTE: the shared ``## Security Research Mindset`` partial (_mindset.j2)
        # IS intentionally present in flat now — it is the phase-neutral
        # methodology shared with the BEF workers. What must stay out is the
        # tree-topology framing: role/operation templates, the per-phase worker
        # step-by-step shells, and boss decomposition mechanics.
        assert "You are an agent in a recursive multi-agent hierarchy" not in prompt
        assert "You are a **WORKER** agent" not in prompt
        assert "## Task Execution" not in prompt
        assert "## Builder Worker — Step-by-Step" not in prompt
        assert "Create exactly 4 subtasks" not in prompt  # boss.j2 decomposition leak

    def test_build_flat_prompt_includes_subagent_note_when_enabled(self) -> None:
        # Given: a flat-mode PromptBuilder.
        builder = self._builder()

        # When: building with subagent_enabled=True (A1).
        prompt = builder.build_flat_prompt(
            task_description="openjpeg.cve-2016-7445",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
            subagent_enabled=True,
        )

        # Then: the Task-tool capability note appears exactly once, between
        # the pipeline overview and the task block.
        assert prompt.count(FLAT_SUBAGENT_NOTE) == 1
        pipeline_marker = "## Vulnerability Reproduction — 4-Phase Process"
        task_marker = "<task>\nopenjpeg.cve-2016-7445\n</task>"
        assert prompt.index(pipeline_marker) < prompt.index(FLAT_SUBAGENT_NOTE)
        assert prompt.index(FLAT_SUBAGENT_NOTE) < prompt.index(task_marker)

    def test_build_flat_prompt_omits_subagent_note_when_disabled(self) -> None:
        # Given: a flat-mode PromptBuilder.
        builder = self._builder()

        # When: building with subagent_enabled=False (A2).
        prompt = builder.build_flat_prompt(
            task_description="openjpeg.cve-2016-7445",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
            subagent_enabled=False,
        )

        # Then: the note is absent.
        assert FLAT_SUBAGENT_NOTE not in prompt

    def test_build_flat_prompt_without_cve_returns_just_task_block(self) -> None:
        # Given: a flat-mode PromptBuilder with no CVE context.
        builder = self._builder()

        # When: building without a domain context.
        prompt = builder.build_flat_prompt(
            task_description="openjpeg.cve-2016-7445",
            agent_id=uuid4(),
            domain_context=None,
            subagent_enabled=False,
        )

        # Then: the strategy returns None, so only the task block survives.
        assert prompt == "<task>\nopenjpeg.cve-2016-7445\n</task>"

    def test_detached_exec_guidance_shared_across_flat_and_workers(self) -> None:
        """Long-command (detached-exec) guidance lives in the shared phase
        partials, so it must render identically into the flat baseline and the
        BEF workers. The guidance fixes the B4 ``shell_in_container`` 300 s
        timeout churn; if it silently rendered in only one arm, the N-vs-B input
        parity that makes the cost comparison fair would break.
        """
        # Given: a flat-mode PromptBuilder + the SEC-bench strategy.
        builder = self._builder()
        cve = make_test_cve_instance()

        # When: rendering the flat baseline and each BEF worker branch.
        flat = builder.build_flat_prompt(
            task_description="demo.cve-2024-0001",
            agent_id=uuid4(),
            domain_context=cve,
            subagent_enabled=False,
        )
        workers = {
            branch: builder.build_worker_prompt(
                task_description=f"[{branch}] run the {branch.lower()} phase",
                domain_context=cve,
                briefing=None,
            )
            for branch in ("Builder", "Exploiter", "Fixer")
        }

        marker = "Run long commands detached"
        # Then: the flat baseline renders all three long-command phases, each
        # carrying the principle and its concrete detached-launch sentinel.
        assert marker in flat
        for sentinel in (
            "/testcase/build.exit",
            "/testcase/repro_loop.exit",
            "/testcase/fix_loop.exit",
        ):
            assert sentinel in flat, f"flat prompt missing detached sentinel {sentinel}"

        # And: each BEF worker renders the same principle in its own phase
        # partial, with the phase-specific detached command verbatim.
        for branch, prompt in workers.items():
            assert marker in prompt, f"{branch} worker missing detached-exec guidance"
        assert "/testcase/build.exit" in workers["Builder"]
        assert "/testcase/repro_loop.exit" in workers["Exploiter"]
        assert "/testcase/fix_loop.exit" in workers["Fixer"]
