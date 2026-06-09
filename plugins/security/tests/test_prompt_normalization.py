"""Prompt-normalization invariants for the BEF 4-arm comparison.

The flat baseline and the BEF workers must share ONE task-spec so every arm
emits byte-identical ``/testcase/`` artifacts. This is enforced structurally:
both consumers ``{% include %}`` the same per-phase partials under
``prompts/domains/secbench/phases/``. These tests prove the two properties
that comparability rests on:

(a) each phase's deliverable+gate block is BYTE-IDENTICAL between the flat
    prompt and the matching BEF worker branch, and
(b) the flat prompt carries ZERO sibling/peer/handoff wording — the
    single-agent baseline cannot contain tree-topology coordination language.
"""

import re
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.services import PromptBuilder
from plugins.security import CVEInstance, SecBenchPromptStrategy


def _prompts_dir() -> Path:
    docker_path = Path("/app/prompts")
    if docker_path.exists():
        return docker_path
    local_path = Path(__file__).parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    return Path("prompts")


PROMPTS_DIR = _prompts_dir()

# Maps each BEF worker branch bracket to the shared phase partial it includes.
_BRANCH_TO_PHASE = (
    ("[Builder]", "build"),
    ("[Exploiter]", "exploit"),
    ("[Fixer]", "fix"),
    ("[Reporter]", "report"),
)


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
    return template.render(include_validation_gate=True, **cve.to_template_context())


class TestPhaseBlocksAreByteIdenticalAcrossArms:
    """Each phase's deliverable+gate block is identical in flat and BEF worker."""

    def test_each_phase_partial_is_verbatim_in_flat_and_worker(self) -> None:
        # Given: a flat prompt and one BEF worker prompt per branch, all on the
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
        for _, phase in _BRANCH_TO_PHASE:
            assert len(_render_phase_partial(builder, cve, phase)) > 200


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
