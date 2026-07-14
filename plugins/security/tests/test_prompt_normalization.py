"""Prompt-normalization invariants for flat and hierarchical execution.

The flat baseline and hierarchical workers must share one task specification so every topology
emits byte-identical ``/testcase/`` artifacts. This is enforced structurally:
the flat prompt and whole-phase worker fallbacks ``{% include %}`` the same per-phase partials under
``prompts/domains/secbench/phases/``. These tests prove the two properties
that comparability rests on:

(a) each whole-phase worker fallback's deliverable+gate block is byte-identical
    to the matching block in the flat prompt, and
(b) the flat prompt carries ZERO sibling/peer/handoff wording — the
    single-agent baseline cannot contain tree-topology coordination language.
"""

import re
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services import PromptBuilder
from plugins.security import CVEInstance, SecBenchPromptStrategy
from plugins.security.deliverables import (
    ARTIFACT_DIRS,
    ARTIFACT_PATHS,
    EXPLOIT_VALIDATION_FIELDS,
    HIERARCHICAL_ONLY,
    PATCH_VALIDATION_FIELDS,
    PHASE_COMMANDS,
    REQUIRED_FILES,
    REQUIRED_FILES_WITH_PURPOSE,
    ROOT_CAUSE_BLOCK_FIELDS,
    VALIDATION_REQUIRED,
)


def _prompts_dir() -> Path:
    docker_path = Path("/app/prompts")
    if docker_path.exists():
        return docker_path
    local_path = Path(__file__).parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    return Path("prompts")


PROMPTS_DIR = _prompts_dir()

# Explicit role labels use focused role templates. Only these legacy whole-phase labels include
# the complete shared phase partial.
_BRANCH_TO_PHASE = (
    ("[Builder]", "build"),
    ("[Exploiter]", "exploit"),
    ("[Fixer]", "fix"),
)
_ALL_PHASES = ("build", "exploit", "fix", "report")


def _cve() -> CVEInstance:
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


def _builder() -> PromptBuilder:
    return PromptBuilder(
        template_dir=PROMPTS_DIR,
        default_tool="claude_code",
        strategy=SecBenchPromptStrategy(),
    )


def _render_phase_partial(builder: PromptBuilder, cve: CVEInstance, phase: str) -> str:
    """Render a phase partial standalone with the SAME context both consumers pass.

    Callers (flat + worker) both render with ``include_validation_gate=True``
    plus the CVE template context, so a standalone render of the partial is the
    ground-truth block that must appear verbatim in both prompts.
    """
    template = builder.env.get_template(f"domains/secbench/phases/{phase}.j2")
    contract_ctx = {
        "artifact_dirs": ARTIFACT_DIRS,
        "artifact_paths": ARTIFACT_PATHS,
        "phase_commands": PHASE_COMMANDS,
        "required_files": REQUIRED_FILES,
        "required_files_with_purpose": REQUIRED_FILES_WITH_PURPOSE,
        "validation_required": VALIDATION_REQUIRED,
        "hierarchical_only": HIERARCHICAL_ONLY,
        "root_cause_block_fields": ROOT_CAUSE_BLOCK_FIELDS,
        "exploit_validation_fields": EXPLOIT_VALIDATION_FIELDS,
        "patch_validation_fields": PATCH_VALIDATION_FIELDS,
    }
    return template.render(
        include_validation_gate=True,
        **cve.to_template_context(),
        **contract_ctx,
    )


class TestPhaseBlocksAreByteIdenticalAcrossTopologies:
    """Each whole-phase fallback shares its deliverable block with flat execution."""

    def test_each_phase_partial_is_verbatim_in_flat_and_worker(self) -> None:
        # Given: a flat prompt and one whole-phase worker prompt per branch, all on the
        # same CVE, built through PromptBuilder + SecBenchPromptStrategy.
        builder = _builder()
        cve = _cve()

        flat_prompt = builder.build_flat_prompt(
            task_description="demo.cve-2024-0001",
            agent_id=uuid4(),
            domain_context=cve,
        )
        worker_prompts = {
            bracket: builder.build_worker_prompt(
                task_description=f"{bracket} do the phase",
                domain_context=cve,
                briefing=None,
            )
            for bracket, _ in _BRANCH_TO_PHASE
        }

        for bracket, phase in _BRANCH_TO_PHASE:
            # When: extracting the canonical per-phase block (the shared partial).
            block = _render_phase_partial(builder, cve, phase)

            # Then: it appears byte-identically in BOTH the flat prompt and the
            # matching BEF worker branch — same task spec, same deliverables,
            # same verdict gate.
            assert block in flat_prompt, (
                f"Phase {phase!r} block is not a verbatim substring of the flat "
                f"prompt — flat and worker no longer share the partial."
            )
            assert block in worker_prompts[bracket], (
                f"Phase {phase!r} block is not a verbatim substring of the "
                f"{bracket} worker prompt — the worker shell stopped including "
                f"the shared partial."
            )

        # And: the blocks are non-trivial (the gate text is actually rendered).
        for phase in _ALL_PHASES:
            assert len(_render_phase_partial(builder, cve, phase)) > 200

    def test_each_phase_partial_has_numbered_goal_and_step_headings(self) -> None:
        # Given: the shared phase partials used by flat and worker prompts.
        builder = _builder()
        cve = _cve()
        builder_prompt = builder.build_worker_prompt(
            task_description="[Builder] build the supplied baseline",
            domain_context=cve,
            briefing=None,
        )

        # When: rendering each phase partial directly.
        blocks = {
            phase: _render_phase_partial(builder, cve, phase)
            for phase in _ALL_PHASES
        }

        # Then: each phase presents its goal as a numbered list.
        for phase, block in blocks.items():
            assert "### Goal\n\n1. " in block, phase
            assert "\n2. " in block, phase

        # And: command-heavy paragraphs sit under explicit headings.
        for phase, block in blocks.items():
            assert "### Mandatory Requirements" in block, phase
            assert "### Deliverables" in block, phase
        assert "### Validation Steps" in blocks["build"]
        assert "### Validation Steps" in blocks["exploit"]
        assert "### Validation Steps" in blocks["fix"]
        assert "### Validation Checks" in blocks["report"]

        # And: Builder leads with the secb build contract, not direct build.sh validation.
        assert (
            "1. Run `secb build` from the CVE work directory to build the selected base commit"
            in blocks["build"]
        )
        assert ARTIFACT_PATHS["binary_paths"] in blocks["build"]
        assert "Use the exact supplied base commit" in builder_prompt
        assert "If a more suitable commit exists" not in builder_prompt
        assert "Treat `/usr/local/bin/secb build()` as an optional reference/helper" not in blocks[
            "build"
        ]

        # And: Exploiter leads with selected-PoC discovery and secb repro,
        # without relying on helper internals or poc* naming.
        assert (
            "1. Select the shipped PoC under `/testcase` by inspecting filenames, file types, and CVE relevance"
            in blocks["exploit"]
        )
        assert "Run `secb repro`" in blocks["exploit"]
        assert ARTIFACT_PATHS["poc_path"] in blocks["exploit"]
        assert "Only create a new PoC when no provided PoC exists" in blocks["exploit"]
        assert "Treat `/usr/local/bin/secb repro()` as an optional reference/helper" not in blocks[
            "exploit"
        ]


class TestFlatPromptHasNoSiblingWording:
    """The single-agent baseline must contain no tree-topology coordination wording."""

    def test_flat_prompt_excludes_sibling_peer_handoff_wording(self) -> None:
        # Given: a flat-mode prompt built from a CVE context.
        builder = _builder()
        cve = _cve()

        prompt = builder.build_flat_prompt(
            task_description="demo.cve-2024-0001",
            agent_id=uuid4(),
            domain_context=cve,
        )

        # When: scanning case-insensitively for coordination vocabulary that
        # only makes sense inside a multi-agent tree.
        lowered = prompt.lower()
        banned = (
            "sibling",
            "peer",
            "handoff",
            "hand off",
            "<context-update>",
            "manager",
            "downstream worker",
            "upstream",
        )

        # Then: none of it leaks into the single-agent baseline.
        for token in banned:
            assert token not in lowered, (
                f"Flat baseline contains tree-topology wording {token!r} — the "
                f"shared partials or the single-agent wrapper leaked coordination "
                f"language into the baseline."
            )

        # And: catch ANY hand-off phrasing the fixed token list would miss
        # (e.g. "hand work off"), per the normalization contract.
        assert re.search(r"hand\s*\w*\s*off", lowered) is None, (
            "Flat baseline contains hand-off phrasing — the single-agent wrapper "
            "leaked delegation language into the baseline."
        )

        # And: the worker branch DOES carry that wording (so the test is not
        # vacuously passing because the wording was deleted everywhere).
        worker = builder.build_worker_prompt(
            task_description="[Exploiter] reproduce the crash",
            domain_context=cve,
            briefing=None,
        )
        assert "<context-update>" in worker
        assert "sibling" in worker.lower()


class TestWorkerBranchFailsFastWithoutPhase:
    """A CVE worker with no detectable branch has no phase contract → fail loudly."""

    def test_undetectable_branch_raises_value_error(self) -> None:
        # Given: a CVE worker whose task names no phase and has no ancestry.
        builder = _builder()
        cve = _cve()

        # When/Then: building the worker prompt fails fast rather than emitting
        # a worker without the phase deliverable contract.
        with pytest.raises(ValueError, match="no phase branch could be detected"):
            builder.build_worker_prompt(
                task_description="please do some generic security work",
                domain_context=cve,
                briefing=None,
            )

    def test_non_cve_worker_does_not_fail_fast(self) -> None:
        # Given: a worker with no CVE domain context (the non-security path).
        builder = _builder()

        # When: building without a CVEInstance, the strategy returns None and
        # core falls back to default prompts.
        prompt = builder.build_worker_prompt(
            task_description="please do some generic work",
            domain_context=None,
            briefing=None,
        )

        # Then: no ValueError — the fail-fast guard is scoped to CVE workers only.
        assert "<persona>" in prompt
