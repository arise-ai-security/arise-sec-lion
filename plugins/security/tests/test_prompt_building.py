"""Security-plugin prompt building integration tests."""

from pathlib import Path
from uuid import uuid4

from core.application.services import PromptBuilder, PromptParser
from core.application.services.prompt.prompt_builder import FLAT_SUBAGENT_NOTE
from core.domain.aggregates.agent_session import AgentRole
from core.domain.values.limits import HierarchyLimits
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import CVEInstance, SecBenchPromptStrategy, SecurityDomainPlugin
from plugins.security.prompt_strategy import _detect_branch_from_task, detect_benchmark_branch
from plugins.security.roles import PHASE_ROLES


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


def make_test_cve_instance_with_oracle() -> CVEInstance:
    return make_test_cve_instance().model_copy(
        update={
            "bug_description": "Sparse summary: NULL dereference in parser.",
            "sanitizer_report": (
                "==1==ERROR: AddressSanitizer: SEGV\n"
                "#0 0x123 in gf_filter_pck_new_alloc_internal "
                "/src/demo/filter.c:42\n"
                "SUMMARY: AddressSanitizer: SEGV "
                "/src/demo/filter.c:42 in gf_filter_pck_new_alloc_internal\n"
            ),
            "bug_report": (
                "Expected crash frame: gf_filter_pck_new_alloc_internal "
                "after malformed packet input."
            ),
            "patch": "diff --git a/demo.c b/demo.c\n",
            "candidate_fixes": "commit abc fixes the bug",
            "secb_sh": "repro() { echo GOLDEN-REPRO-SECRET; }",
        }
    )


def test_cve_template_context_excludes_golden_secb_helper() -> None:
    """Golden SEC-bench helper bodies are evaluation data, not prompt data."""
    # Given: a CVE instance with a golden secb helper body.
    cve = make_test_cve_instance().model_copy(
        update={"secb_sh": "#!/bin/bash\necho GOLDEN-REPRO-SECRET\n"}
    )

    # When: building the render-safe template context.
    context = cve.to_template_context()

    # Then: forbidden golden helper content is absent from the prompt context.
    assert "secb_sh" not in context
    assert "GOLDEN-REPRO-SECRET" not in str(context)


def test_cve_template_context_keeps_expected_failure_oracle() -> None:
    """Sanitizer and bug reports are expected-failure oracles, not answer keys."""
    # Given: a CVE with sparse description but detailed expected crash evidence.
    cve = make_test_cve_instance_with_oracle()

    # When: building the render-safe template context.
    context = cve.to_template_context()

    # Then: the expected failure evidence remains available to prompts.
    assert "sanitizer_report" in context
    assert "bug_report" in context
    assert "gf_filter_pck_new_alloc_internal" in str(context)

    # And: answer-bearing solution fields remain blocked.
    assert "patch" not in context
    assert "candidate_fixes" not in context
    assert "secb_sh" not in context


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

    def test_secbench_prompt_renders_expected_sanitizer_oracle_without_solution_fields(
        self,
    ) -> None:
        """Workers need the expected crash oracle but must not see solution artifacts."""
        # Given: a CVE whose exact expected crash is only in sanitizer_report/bug_report.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )
        cve = make_test_cve_instance_with_oracle()

        # When: rendering an Exploiter worker prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Exploiter] reproduce the vulnerability",
            domain_context=cve,
            briefing=None,
        )

        # Then: the exact expected crash oracle is visible.
        assert "gf_filter_pck_new_alloc_internal" in prompt
        assert "SUMMARY: AddressSanitizer: SEGV" in prompt
        assert "Expected crash frame" in prompt

        # And: answer-bearing solution artifacts are still not rendered.
        assert "secb_sh" not in prompt
        assert "candidate_fixes" not in prompt
        assert "GOLDEN-REPRO-SECRET" not in prompt
        assert "commit abc fixes the bug" not in prompt
        assert "diff --git a/demo.c b/demo.c" not in prompt

    def test_secbench_assessment_prompt_renders_prompt_only_role_dependencies(self) -> None:
        # Given: a SEC-bench assessment prompt built from the role catalog.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the assessment prompt.
        prompt = builder.build_assessment_prompt(
            task_description="[Fixer] Produce the patch",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
        )

        # Then: hard dependencies are an explicit prompt contract, not runtime repair.
        assert "The system will not auto-add missing producer roles" in prompt
        assert (
            "`[Forward-Instrumentator]` hard depends_on: `[PoC-Researcher]`"
            in prompt
        )
        assert "`[Patch-Creator]` hard depends_on: `[Root-Cause-Analyst]`" in prompt

        # And: soft inputs are rendered as non-scheduling guidance.
        assert "`[Root-Cause-Analyst]` soft inputs: `[Forward-Instrumentator]`" in prompt
        assert "Do not put soft inputs in JSON `depends_on`" in prompt

    def test_secbench_assessment_constraints_do_not_force_phase_execution(self) -> None:
        # Given: a SEC-bench phase-level assessment at the hierarchy limit.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )
        cve = make_test_cve_instance()
        limits = HierarchyLimits(
            current_depth=1,
            max_depth=1,
            max_children_per_node=3,
            max_retries=0,
            root_id=uuid4(),
            max_total_agents=1,
            current_total_agents=1,
            domain_context=cve,
        )

        # When: rendering the phase-level assessment prompt.
        prompt = builder.build_assessment_prompt(
            task_description="[Builder] Build the project",
            agent_id=uuid4(),
            hierarchy_limits=limits,
            domain_context=cve,
        )

        # Then: resource exhaustion returns an unsatisfiable decision instead of
        # contradicting SEC-bench's "phase-level tasks must decompose" rule.
        assert "domain-specific guidance requires decomposition" in prompt
        assert "constraints_unsatisfiable" in prompt
        assert '**FORCED**: You MUST choose `"execute"`' not in prompt

    def test_secbench_boss_exact_phase_count_yields_to_live_limits(self) -> None:
        # Given: a SEC-bench boss prompt rendered with fewer child slots than phases.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )
        cve = make_test_cve_instance()
        limits = HierarchyLimits(
            current_depth=0,
            max_depth=2,
            max_children_per_node=3,
            max_retries=0,
            root_id=uuid4(),
            max_total_agents=10,
            current_total_agents=1,
            domain_context=cve,
        )

        # When: rendering the boss decomposition prompt.
        prompt = builder.build_boss_delegation_prompt(
            task_description="Run the SEC-bench task",
            agent_id=uuid4(),
            hierarchy_limits=limits,
            domain_context=cve,
        )

        # Then: the SEC-bench exact-four rule is not framed as overriding live
        # system constraints that the generic operation template must enforce.
        assert "unless live system constraints make that impossible" in prompt
        assert "Do **not** raise" not in prompt

    def test_secbench_boss_defers_binary_location_to_binary_paths(self) -> None:
        # Given: a SEC-bench boss decomposition prompt.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )
        cve = make_test_cve_instance()
        limits = HierarchyLimits(
            current_depth=0,
            max_depth=2,
            max_children_per_node=3,
            max_retries=0,
            root_id=uuid4(),
            max_total_agents=10,
            current_total_agents=1,
            domain_context=cve,
        )

        # When: rendering the boss decomposition prompt.
        prompt = builder.build_boss_delegation_prompt(
            task_description="Run the SEC-bench task",
            agent_id=uuid4(),
            hierarchy_limits=limits,
            domain_context=cve,
        )

        # Then: the Builder→Exploiter handoff defers to binary_paths.txt as the
        # sole authority and does not reintroduce a /work/bin default that the
        # exploiter prompt forbids scanning.
        assert "binary_paths.txt" in prompt
        assert "preferred/default" not in prompt

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

    def test_secbench_manager_decomposition_forbids_direct_runtime_commands(self) -> None:
        """Managers must not emit stale direct-build/repro/patch child tasks."""
        # Given: a SEC-bench manager decomposition prompt for a phase-level task.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )
        briefing = Briefing(
            parent_task="Top-level SEC-bench task",
            parent_role="boss",
            ancestry=(
                Ancestor(
                    agent_id=str(uuid4()),
                    role="boss",
                    task_summary="[Builder] Build the benchmark target",
                ),
            ),
        )

        # When: rendering the prompt.
        prompt = builder.build_manager_decomposition_prompt(
            task_description="[Builder] Build the benchmark target",
            agent_id=uuid4(),
            agent_role=AgentRole.MANAGER,
            domain_context=make_test_cve_instance(),
            briefing=briefing,
        )

        # Then: managers are explicitly constrained at the JSON-generation boundary.
        assert "Subtask descriptions, justifications, and success_criteria" in prompt
        assert "must say `secb build`, `secb repro`, or `secb patch`" in prompt
        assert "must not tell children to run `/src/build.sh` directly" in prompt
        assert "must not tell children to run `bash /testcase/repro.sh`" in prompt
        assert "must not tell children to run manual `git apply` patch validation" in prompt
        assert "Build recipe consumed by `secb build`: `/src/build.sh`" in prompt
        assert "Build script: `/src/build.sh`" not in prompt
        assert "Build script provided for compiling with sanitizers" not in prompt
        assert (
            "If domain guidance explicitly names required runtime wrapper commands" in prompt
        )

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

    def test_secbench_exploiter_fails_on_broken_builder_handoff_no_rebuild(self) -> None:
        # Given: a SEC-bench worker prompt for the exploiter phase.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Exploiter] Reproduce the vulnerability",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: a missing/invalid Builder binary is a terminal failure, not a
        # rebuild. Builder and Exploiter share one container, so re-running
        # `secb build` with the same inputs cannot produce a binary the Builder
        # phase could not — the conditional-rebuild permission is gone.
        assert "do not invent a different binary path" in prompt
        assert "Do not create a new build process or rebuild" in prompt
        assert "FAIL with a clear reason instead of rebuilding" in prompt
        assert "Only re-run `secb build` if the declared binary is missing or invalid" not in prompt
        assert "re-run `secb build` if needed" not in prompt
        assert "Repair Builder output only if needed" not in prompt
        # And: the pre-repro binary-verification preamble remains.
        assert "Before editing `/testcase/repro.sh` or running `secb repro`" in prompt
        assert "compiled binaries are at `/work/bin/` — do NOT rebuild from scratch" not in prompt

    def test_secbench_exploiter_treats_binary_paths_as_sole_authority(self) -> None:
        # Given: a SEC-bench Exploiter worker prompt.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Exploiter] Reproduce the vulnerability",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: Exploiter must validate only the Builder-declared binary paths.
        assert "/testcase/binary_paths.txt` is the sole authority" in prompt
        assert "Do not scan `/src/demo`, `/work/bin`, `/src`, or PATH" in prompt
        assert "Sibling binary-path findings are advisory only" in prompt
        assert "must be listed in `/testcase/binary_paths.txt`" in prompt
        assert "also check `/src/demo` and `/work/bin/`" not in prompt
        assert "If a valid binary is present, continue to step 3" not in prompt
        assert "ASan binary path, PoC file, or repro command — **reuse it directly**" not in prompt

    def test_secbench_fixer_rebuild_guidance_separates_setup_from_validation(self) -> None:
        # Given: a SEC-bench worker prompt for the fixer phase.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Fixer] Patch the vulnerability",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: pre-patch setup avoids redundant rebuilds, while post-patch
        # validation still requires a rebuild.
        assert "Before patch development, do not rebuild unless binaries are missing" in prompt
        assert "After applying the patch, `secb build` is mandatory for validation" in prompt

    def test_secbench_builder_commits_local_baseline_for_fixer_diff(self) -> None:
        # Given: a SEC-bench Builder worker prompt.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Builder] Build the project",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: Builder records its source/setup delta, then makes it the local
        # git baseline so Fixer diffs cannot include Builder hunks.
        assert (
            'git diff --no-color "$(cat /testcase/base_commit_hash)" > '
            "/testcase/repo_changes.diff"
        ) in prompt
        assert "[BASE_COMMIT]" not in prompt
        assert "git add -A &&" not in prompt
        assert "Do not use `git add -A`" in prompt
        assert "git apply --index /testcase/repo_changes.diff" in prompt
        assert "cmp /testcase/repo_changes.diff" in prompt
        assert "commit -m arise-builder-baseline" in prompt
        assert "establishes the Builder-adjusted baseline" in prompt
        assert "/testcase/model_patch.diff` can contain only Fixer edits" in prompt

    def test_secbench_fixer_confirms_builder_baseline_before_patch_diff(self) -> None:
        # Given: a SEC-bench Fixer worker prompt.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Fixer] Patch the vulnerability",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: Fixer checks that Builder hunks are already out of the working
        # tree before producing model_patch.diff.
        assert "Confirm Builder baseline first" in prompt
        assert "git status --short" in prompt
        assert "model_patch.diff must contain only Fixer edits" in prompt
        assert "do not generate `/testcase/model_patch.diff`" in prompt

    def test_secbench_fixer_diff_targets_builder_baseline_not_original_base(self) -> None:
        # Given: a SEC-bench Fixer worker prompt.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_worker_prompt(
            task_description="[Fixer] Patch the vulnerability",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: the diff is taken against the committed Builder baseline (HEAD),
        # so the prompt no longer claims a bare diff leaks Builder hunks; it warns
        # against diffing the original base and against truncating a multi-file fix.
        assert "git diff --no-color HEAD --" in prompt
        assert "would re-include Builder's hunks" in prompt
        assert "truncates a multi-file fix" in prompt
        assert "include unrelated builder changes" not in prompt

        # And: the stale "manually reset to base before applying" rule is gone —
        # the reset/replay is owned by the sealed `secb patch` path, not a manual
        # authoring step that could reset away the Builder baseline.
        assert "reset to base commit before applying patch" not in prompt
        assert "Do not manually reset or apply patches while authoring" in prompt

    def test_secbench_fixer_leaf_with_exploit_wording_renders_fixer_prompt(self) -> None:
        # Given: a Fixer leaf (Root-Cause-Analyst) whose own task carries no
        # fixer keyword, under a Fixer parent whose summary legitimately mentions
        # the exploit/repro it validates against.
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
                    agent_id=str(uuid4()), role="boss", task_summary="[Boss] Analyze CVE"
                ),
                Ancestor(
                    agent_id=str(uuid4()),
                    role="manager",
                    task_summary=(
                        "[Fixer] Patch the vulnerability; validate with secb repro "
                        "that the exploit no longer triggers"
                    ),
                ),
            ),
        )
        leaf_task = "[Root-Cause-Analyst] Identify the data-corruption origin and emit the block"

        # When / Then: the [Fixer] bracket on the parent wins over the loose
        # "exploit" keyword, so the leaf renders the Fixer prompt, not Exploiter.
        assert detect_benchmark_branch(briefing) == "fixer"

        prompt = builder.build_worker_prompt(
            task_description=leaf_task,
            domain_context=make_test_cve_instance(),
            briefing=briefing,
        )
        assert "Exploiter Worker" not in prompt
        assert "/testcase/model_patch.diff" in prompt

    def test_secbench_leaf_role_prefix_maps_to_catalog_phase_before_keywords(self) -> None:
        """Catalog role labels are authoritative before loose task-content keywords."""
        # Given: every catalog role with body keywords that point at other phases.
        conflicting_body = {
            "Builder": "fix exploit report after patch validation",
            "Exploiter": "fix build environment setup after report",
            "Fixer": "exploit builder setup report",
            "Reporter": "exploit patch build environment setup",
        }

        # When / Then: the role catalog decides the branch, not loose keywords.
        for phase, roles in PHASE_ROLES.items():
            for role in roles:
                task = f"[{role.name}] {conflicting_body[phase]}"
                assert _detect_branch_from_task(task) == phase.lower()

    def test_secbench_fixer_validation_captures_patch_build_evidence(self) -> None:
        # Given: a SEC-bench Fixer worker prompt with the validation gate on.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )
        prompt = builder.build_worker_prompt(
            task_description="[Fixer] Patch the vulnerability",
            domain_context=make_test_cve_instance(),
            briefing=None,
        )

        # Then: the detached validation captures patch/build exit codes to a
        # readable log instead of `&& ... ; echo done` to /dev/null, so a failed
        # apply/rebuild stays visible (PATCH_APPLY_STATUS / BUILD_STATUS).
        assert "patch_exit=$patch_rc" in prompt
        assert "build_exit=$build_rc" in prompt
        assert "/testcase/fix_loop.log" in prompt
        assert "secb patch && secb build" not in prompt
        assert "Run `secb build`, run `secb repro`" in prompt
        assert "Rebuild with `/src/build.sh`" not in prompt

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

    def test_secbench_bef_prompts_use_secb_commands_as_primary_contract(self) -> None:
        """Builder/Exploiter/Fixer must validate through secb verbs, not helper internals."""

        # Given: SEC-bench prompts for the three executable phases.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        prompts = {
            phase: builder.build_worker_prompt(
                task_description=f"[{phase}] run the phase",
                domain_context=make_test_cve_instance(),
                briefing=None,
            )
            for phase in ("Builder", "Exploiter", "Fixer")
        }
        combined = "\n".join(prompts.values())

        # Then: each phase leads with the SEC-bench command contract.
        assert "secb build" in prompts["Builder"]
        assert "secb repro" in prompts["Exploiter"]
        assert "secb patch" in prompts["Fixer"]

        # And: prompt text does not ask agents to inspect golden helper internals
        # or validate primarily through direct scripts.
        forbidden = (
            "Treat `/usr/local/bin/secb build()` as an optional reference/helper",
            "Treat `/usr/local/bin/secb repro()` as an optional reference/helper",
            "read its `repro()` function",
            "port the trigger command",
            "bash /testcase/repro.sh",
            "/src/build.sh; echo \"exit=$?\"",
            "Rebuild with `/src/build.sh`",
            "Before writing or running repro.sh",
            "rebuild, run PoC",
            "git apply --check /testcase/model_patch.diff && git apply",
        )
        for text in forbidden:
            assert text not in combined

    def test_secbench_prompts_render_selected_poc_and_binary_path_artifacts(self) -> None:
        """Prompts must render the new handoff artifacts and drop stale mandatory files."""

        # Given: the flat prompt, which contains all phase contracts.
        builder = PromptBuilder(
            template_dir=PROMPTS_DIR,
            default_tool="claude_code",
            strategy=SecBenchPromptStrategy(),
        )

        # When: rendering the prompt.
        prompt = builder.build_flat_prompt(
            task_description="demo.cve-2024-0001",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
        )

        # Then: the mandatory artifact list includes exact binary and selected-PoC paths.
        assert "/testcase/binary_paths.txt" in prompt
        assert "/testcase/poc_path.txt" in prompt

        # And: stale mandatory artifact names are not rendered as contract requirements.
        assert "/testcase/poc*" not in prompt
        assert "/testcase/packages.txt" not in prompt


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

    def test_build_flat_prompt_includes_builder_baseline_contract(self) -> None:
        # Given: a flat-mode PromptBuilder.
        builder = self._builder()

        # When: building the flat prompt.
        prompt = builder.build_flat_prompt(
            task_description="openjpeg.cve-2016-7445",
            agent_id=uuid4(),
            domain_context=make_test_cve_instance(),
            subagent_enabled=False,
        )

        # Then: the shared build phase creates the Builder-adjusted baseline.
        assert "arise-builder-baseline" in prompt
        assert "git apply --index /testcase/repo_changes.diff" in prompt
        assert "Do not use `git add -A`" in prompt

        # And: the shared fix phase generates model_patch.diff from that baseline.
        assert "git diff --no-color HEAD -- <file1> [<file2> ...]" in prompt
        assert "would re-include Builder's hunks" in prompt

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
