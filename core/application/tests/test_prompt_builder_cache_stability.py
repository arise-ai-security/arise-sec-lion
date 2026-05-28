"""Cache-stability invariant for PromptBuilder's stable prefix.

The repo's multi-provider cache architecture relies on the **byte-identity of
the stable prefix** that ``PromptBuilder.build_*_prompt(...)`` produces for a
fixed cacheable tuple. Any future change that drifts the prefix bytes within
a cell silently breaks Anthropic / OpenAI / Qwen / Gemini prefix-cache hits.
This test is the contract: if it fails, the cache is broken.

The volatile suffix appended by ``PromptBuilder._append_volatile_suffix`` is
*not* under this contract — it carries per-task content (user_prompt, scope,
sibling, briefing, task_description, workspace_context) and intentionally
varies per invocation.

**Cacheable tuples differ per role**:

- ``pending`` / ``assess`` and ``boss`` / ``decomposition``:
  ``{role, operation, CVE}``. The strategy passes ``phase=None`` to
  ``cve.j2``, which renders ALL phase sections; branch does not affect the
  stable prefix.
- ``manager`` / ``decomposition`` and ``worker`` / ``execution``:
  ``{role, operation, CVE, branch}``. The strategy passes
  ``phase=<branch>`` to ``cve.j2``, which filters sections by phase; for
  worker prompts, ``worker/<branch>.j2`` is additionally rendered into the
  stable prefix.

Two task_descriptions with the same bracket prefix (e.g. both
``[exploiter] ...``) select the same branch via ``_detect_branch_from_task``,
so the bracket prefix must be held constant inside each manager/worker cell
while the rest of task_description varies.
"""

from pathlib import Path
from uuid import uuid4

from core.application.services import PromptBuilder
from core.application.services.prompt.prompt_strategy import SubtaskScope
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import CVEInstance, SecBenchPromptStrategy


# Markers that delimit the start of the volatile suffix appended by
# ``PromptBuilder._append_volatile_suffix``. See the suffix order in
# ``core/application/services/prompt/prompt_builder.py:148-180``: user_prompt
# (free text), scope, sibling (parent_context or sibling_tasks), briefing,
# task, workspace.
_VOLATILE_MARKERS: tuple[str, ...] = (
    "\n\n<user_prompt>",
    "\n\n<scope>",
    "\n\n    <parent_context>",
    "\n\n    <sibling_tasks>",
    "\n\n<briefing>",
    "\n\n<task>",
    "\n\n<workspace>",
)


class _NoVolatileMarkerError(AssertionError):
    """Raised when ``_stable_prefix`` cannot find any volatile-suffix marker.

    This is an integrity failure: the suffix structure changed without
    updating ``_VOLATILE_MARKERS``. A test that returns "the entire prompt"
    as the stable prefix would silently pass, hiding cache drift.
    """


def _stable_prefix(prompt: str) -> str:
    """Return everything before the first volatile-suffix marker.

    Raises ``_NoVolatileMarkerError`` if no marker is found — every prompt
    built via ``PromptBuilder.build_*_prompt`` ALWAYS contains a ``<task>``
    block in its volatile suffix (``_append_volatile_suffix`` line 173), so
    the absence of every marker signals a structural change that must update
    this test in the same commit.
    """
    earliest = len(prompt)
    found = False
    for marker in _VOLATILE_MARKERS:
        idx = prompt.find(marker)
        if 0 <= idx < earliest:
            earliest = idx
            found = True
    if not found:
        raise _NoVolatileMarkerError(
            "No volatile-suffix marker found in rendered prompt. "
            "Either the suffix structure changed (update _VOLATILE_MARKERS) "
            "or the strategy stopped appending a <task> block (which would "
            "itself be a bug in PromptBuilder._append_volatile_suffix)."
        )
    return prompt[:earliest]


def _prompts_dir() -> Path:
    docker_path = Path("/app/prompts")
    if docker_path.exists():
        return docker_path
    local_path = Path(__file__).parent.parent.parent.parent / "prompts"
    if local_path.exists():
        return local_path
    return Path("prompts")


PROMPTS_DIR = _prompts_dir()


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


def _briefing(
    branch_bracket: str,
    *,
    parent_task: str = "Top-level task",
    justification_text: str = "default justification",
) -> Briefing:
    return Briefing(
        parent_task=parent_task,
        parent_role="manager",
        ancestry=(
            Ancestor(
                agent_id=str(uuid4()),
                role="manager",
                task_summary=f"{branch_bracket} ancestor summary",
            ),
        ),
        subtask_justification={"plan": justification_text},
    )


# ===== Worker prompt: cacheable tuple is {role, op, CVE, branch} =====


class TestWorkerPromptStableByBranch:
    """For each worker/<branch>.j2 cell, varying volatile inputs must NOT drift the prefix."""

    def _assert_prefix_stable(self, branch_bracket: str) -> None:
        # Given: two worker prompts sharing branch + CVE, varying everything else.
        builder = _builder()
        cve = _cve()

        prompt_a = builder.build_worker_prompt(
            task_description=f"{branch_bracket} Reproduce the crash with input A",
            domain_context=cve,
            briefing=_briefing(branch_bracket, justification_text="approach A"),
        )
        prompt_b = builder.build_worker_prompt(
            task_description=f"{branch_bracket} Reproduce the crash via fuzzer input B",
            domain_context=cve,
            briefing=_briefing(
                branch_bracket,
                parent_task="Different parent",
                justification_text="approach B with extra detail",
            ),
            workspace_context="files: a.txt, b.txt",
        )

        # When: extracting the stable prefix of each.
        prefix_a = _stable_prefix(prompt_a)
        prefix_b = _stable_prefix(prompt_b)

        # Then: the two stable prefixes are byte-identical.
        assert prefix_a == prefix_b, (
            f"Stable prefix drifted within {branch_bracket} worker cell.\n"
            f"A length: {len(prefix_a)}; B length: {len(prefix_b)}\n"
            f"Earliest divergence: {_first_diff(prefix_a, prefix_b)}"
        )

        # And: prefix is non-trivial (the worker/<branch>.j2 is included).
        assert len(prefix_a) > 1000, (
            f"Prefix suspiciously short ({len(prefix_a)} chars) — strategy "
            f"may not have rendered worker/<branch>.j2 into the stable region."
        )

    def test_exploiter_cell(self) -> None:
        self._assert_prefix_stable("[Exploiter]")

    def test_builder_cell(self) -> None:
        self._assert_prefix_stable("[Builder]")

    def test_fixer_cell(self) -> None:
        self._assert_prefix_stable("[Fixer]")

    def test_reporter_cell(self) -> None:
        self._assert_prefix_stable("[Reporter]")


class TestWorkerPrefixDiffersAcrossBranches:
    """Across worker branches, the stable prefix MUST differ — branch is in the cacheable tuple."""

    def test_exploiter_vs_fixer_prefix_differs(self) -> None:
        builder = _builder()
        cve = _cve()

        prompt_exploiter = builder.build_worker_prompt(
            task_description="[Exploiter] task",
            domain_context=cve,
            briefing=_briefing("[Exploiter]"),
        )
        prompt_fixer = builder.build_worker_prompt(
            task_description="[Fixer] task",
            domain_context=cve,
            briefing=_briefing("[Fixer]"),
        )

        prefix_e = _stable_prefix(prompt_exploiter)
        prefix_f = _stable_prefix(prompt_fixer)

        assert prefix_e != prefix_f
        assert len(prefix_e) > 1000
        assert len(prefix_f) > 1000


# ===== Manager prompt: cacheable tuple is {role, op, CVE, branch} =====
# Manager is branch-sensitive because the strategy passes phase=branch to
# cve.j2, which filters phase sections (see prompts/domains/secbench/cve.j2).


class TestManagerPromptStableByBranch:
    """For each manager branch, varying volatile inputs must NOT drift the prefix."""

    def _assert_prefix_stable(self, branch_bracket: str) -> None:
        builder = _builder()
        cve = _cve()

        prompt_a = builder.build_manager_decomposition_prompt(
            task_description=f"{branch_bracket} decompose path A",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing(branch_bracket, justification_text="plan A"),
        )
        prompt_b = builder.build_manager_decomposition_prompt(
            task_description=f"{branch_bracket} decompose path B with extra detail",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing(
                branch_bracket,
                parent_task="Other parent",
                justification_text="plan B with much more elaboration",
            ),
            scope=SubtaskScope(target_paths=("src/parser.c",), symbols=("parse_header",)),
        )

        prefix_a = _stable_prefix(prompt_a)
        prefix_b = _stable_prefix(prompt_b)

        assert prefix_a == prefix_b, (
            f"Stable prefix drifted within {branch_bracket} manager cell.\n"
            f"A length: {len(prefix_a)}; B length: {len(prefix_b)}\n"
            f"Earliest divergence: {_first_diff(prefix_a, prefix_b)}"
        )
        assert len(prefix_a) > 1000

    def test_exploiter_cell(self) -> None:
        self._assert_prefix_stable("[Exploiter]")

    def test_builder_cell(self) -> None:
        self._assert_prefix_stable("[Builder]")

    def test_fixer_cell(self) -> None:
        self._assert_prefix_stable("[Fixer]")


class TestManagerPrefixDiffersAcrossBranches:
    """Manager: cve.j2 filters by phase, so different branches → different prefix bytes."""

    def test_exploiter_vs_fixer_prefix_differs(self) -> None:
        builder = _builder()
        cve = _cve()

        prompt_exploiter = builder.build_manager_decomposition_prompt(
            task_description="[Exploiter] decompose",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing("[Exploiter]"),
        )
        prompt_fixer = builder.build_manager_decomposition_prompt(
            task_description="[Fixer] decompose",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing("[Fixer]"),
        )

        prefix_e = _stable_prefix(prompt_exploiter)
        prefix_f = _stable_prefix(prompt_fixer)

        assert prefix_e != prefix_f, (
            "Manager prefix did not differ across branches — but cve.j2 "
            "filters by phase, so it SHOULD differ. Either the strategy "
            "stopped passing phase=branch or cve.j2 stopped filtering."
        )


# ===== Assessment prompt: cacheable tuple is {role, op, CVE} only =====
# Strategy passes phase=None to cve.j2; cve.j2 renders all phase sections.
# Branch does NOT affect the stable prefix.


class TestAssessmentPromptStable:
    def test_stable_prefix_does_not_drift_across_volatile_inputs(self) -> None:
        builder = _builder()
        cve = _cve()

        prompt_a = builder.build_assessment_prompt(
            task_description="Decide whether to execute or decompose this task",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing("[Builder]"),
        )
        prompt_b = builder.build_assessment_prompt(
            task_description="Choose between execute and decompose for the given task",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing("[Exploiter]", justification_text="different rationale"),
            scope=SubtaskScope(target_paths=("src/other.c",)),
        )

        prefix_a = _stable_prefix(prompt_a)
        prefix_b = _stable_prefix(prompt_b)

        assert prefix_a == prefix_b, (
            "Assessment prefix drifted under varied volatile inputs.\n"
            f"Earliest divergence: {_first_diff(prefix_a, prefix_b)}"
        )
        assert len(prefix_a) > 500

    def test_stable_prefix_branch_insensitive(self) -> None:
        """Assessment: phase=None in cve.j2, so changing branch must NOT shift prefix."""
        builder = _builder()
        cve = _cve()

        prompt_exploiter = builder.build_assessment_prompt(
            task_description="[Exploiter] decide",
            agent_id=uuid4(),
            domain_context=cve,
        )
        prompt_fixer = builder.build_assessment_prompt(
            task_description="[Fixer] decide",
            agent_id=uuid4(),
            domain_context=cve,
        )

        # Both render the SAME cve.j2 sections (all of them, since phase=None).
        # The task_description differs but goes into the volatile <task> block.
        assert _stable_prefix(prompt_exploiter) == _stable_prefix(prompt_fixer), (
            "Assessment prefix changed with branch — but cve.j2 should render "
            "all phase sections (phase=None) for assessment."
        )


# ===== Boss prompt: cacheable tuple is {role, op, CVE} only =====
# Same reason as assessment: strategy passes phase=None to cve.j2.


class TestBossPromptStable:
    def test_stable_prefix_does_not_drift_across_volatile_inputs(self) -> None:
        builder = _builder()
        cve = _cve()

        prompt_a = builder.build_boss_delegation_prompt(
            task_description="Root SEC-bench task A",
            agent_id=uuid4(),
            domain_context=cve,
        )
        prompt_b = builder.build_boss_delegation_prompt(
            task_description="Root SEC-bench task B with different framing",
            agent_id=uuid4(),
            domain_context=cve,
            parent_task="N/A (boss has no parent)",
        )

        prefix_a = _stable_prefix(prompt_a)
        prefix_b = _stable_prefix(prompt_b)

        assert prefix_a == prefix_b, (
            "Boss prefix drifted under varied volatile inputs.\n"
            f"Earliest divergence: {_first_diff(prefix_a, prefix_b)}"
        )
        assert len(prefix_a) > 500


# ===== Cross-cell invariants =====


class TestCrossBranchSharedSubPrefix:
    """Across worker branches with same {role, op, CVE}, the shared sub-prefix
    (system + role + operation + CVE-display-up-to-phase-filter) MUST be
    byte-identical. This enables cross-branch cache reuse up to the divergence
    point at the phase-filtered section in cve.j2 and worker/<branch>.j2.
    """

    def test_all_worker_branches_share_common_sub_prefix(self) -> None:
        builder = _builder()
        cve = _cve()

        prompts = {
            branch: _stable_prefix(
                builder.build_worker_prompt(
                    task_description=f"{branch} task",
                    domain_context=cve,
                    briefing=_briefing(branch),
                )
            )
            for branch in ("[Builder]", "[Exploiter]", "[Fixer]", "[Reporter]")
        }

        lcp = _longest_common_prefix(list(prompts.values()))

        assert len(lcp) > 500, (
            f"Cross-branch worker LCP is too short ({len(lcp)} chars) — "
            "system/role/operation content is not byte-identical across branches."
        )


class TestSystemBlockStableAcrossRoles:
    """For the same CVE, the system block invariant content (apart from the
    {{ agent_role }} substitution) MUST be byte-identical across roles."""

    def test_system_block_invariant_shared_across_roles(self) -> None:
        builder = _builder()
        cve = _cve()

        assess_prompt = builder.build_assessment_prompt(
            task_description="Decide",
            agent_id=uuid4(),
            domain_context=cve,
        )
        worker_prompt = builder.build_worker_prompt(
            task_description="[Exploiter] task",
            domain_context=cve,
            briefing=_briefing("[Exploiter]"),
        )

        system_assess = _extract_system_block(assess_prompt)
        system_worker = _extract_system_block(worker_prompt)

        assert system_assess != ""
        assert system_worker != ""

        # The roles cause an agent_role substitution inside the block.
        # The common SUFFIX after that substitution must be byte-stable.
        common_suffix = _longest_common_suffix([system_assess, system_worker])
        assert len(common_suffix) > 50, (
            "system.j2 invariant content (apart from agent_role substitution) "
            "is not byte-stable across roles."
        )


class TestVolatileChangesDoNotDriftPrefix:
    """Per-input volatile changes (briefing, scope, sibling, workspace) must
    not affect the stable prefix on the worker role."""

    def test_briefing_change_does_not_drift_worker_prefix(self) -> None:
        builder = _builder()
        cve = _cve()

        prompt_no_briefing = builder.build_worker_prompt(
            task_description="[Builder] task",
            domain_context=cve,
            briefing=None,
        )
        prompt_with_briefing = builder.build_worker_prompt(
            task_description="[Builder] task",
            domain_context=cve,
            briefing=_briefing("[Builder]"),
        )

        assert _stable_prefix(prompt_no_briefing) == _stable_prefix(prompt_with_briefing)

    def test_scope_change_does_not_drift_manager_prefix(self) -> None:
        builder = _builder()
        cve = _cve()

        scope = SubtaskScope(target_paths=("src/parser.c",), symbols=("parse_header",))
        prompt_no_scope = builder.build_manager_decomposition_prompt(
            task_description="[Exploiter] decompose",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing("[Exploiter]"),
        )
        prompt_with_scope = builder.build_manager_decomposition_prompt(
            task_description="[Exploiter] decompose",
            agent_id=uuid4(),
            domain_context=cve,
            briefing=_briefing("[Exploiter]"),
            scope=scope,
        )

        assert _stable_prefix(prompt_no_scope) == _stable_prefix(prompt_with_scope)

    def test_workspace_change_does_not_drift_worker_prefix(self) -> None:
        builder = _builder()
        cve = _cve()

        prompt_no_workspace = builder.build_worker_prompt(
            task_description="[Fixer] task",
            domain_context=cve,
            briefing=_briefing("[Fixer]"),
        )
        prompt_with_workspace = builder.build_worker_prompt(
            task_description="[Fixer] task",
            domain_context=cve,
            briefing=_briefing("[Fixer]"),
            workspace_context="file_a.c, file_b.c, file_c.h",
        )

        assert _stable_prefix(prompt_no_workspace) == _stable_prefix(prompt_with_workspace)


# ===== Marker-list integrity =====


class TestVolatileMarkerListIntegrity:
    """Ensure ``_VOLATILE_MARKERS`` covers every actual volatile-suffix
    starter used by ``PromptBuilder._append_volatile_suffix``. A future
    change that adds a new volatile element with a new wrapper must update
    ``_VOLATILE_MARKERS`` — this test catches a silent drift."""

    def test_every_full_prompt_has_at_least_one_volatile_marker(self) -> None:
        """Every build_*_prompt result MUST contain at least one marker.

        This is enforced indirectly: ``_stable_prefix`` raises
        ``_NoVolatileMarkerError`` when no marker is found. Build prompts for
        all role/operation combos and confirm the helper succeeds.
        """
        builder = _builder()
        cve = _cve()

        # Build one prompt per role/operation combination with realistic inputs.
        prompts = [
            builder.build_assessment_prompt(
                task_description="task", agent_id=uuid4(), domain_context=cve
            ),
            builder.build_boss_delegation_prompt(
                task_description="task", agent_id=uuid4(), domain_context=cve
            ),
            builder.build_manager_decomposition_prompt(
                task_description="[Builder] task",
                agent_id=uuid4(),
                domain_context=cve,
                briefing=_briefing("[Builder]"),
            ),
            builder.build_worker_prompt(
                task_description="[Exploiter] task",
                domain_context=cve,
                briefing=_briefing("[Exploiter]"),
            ),
        ]
        for prompt in prompts:
            # If any prompt lacks a marker, _stable_prefix raises.
            _stable_prefix(prompt)


# ---------- helpers ----------


def _first_diff(a: str, b: str) -> str:
    """Return a short snippet showing where two strings first diverge."""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            start = max(0, i - 40)
            return f"@ char {i}: ...{a[start : i + 40]!r} vs ...{b[start : i + 40]!r}"
    return f"@ char {min(len(a), len(b))}: one is a prefix of the other"


def _longest_common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    base = strings[0]
    for i in range(len(base)):
        for other in strings[1:]:
            if i >= len(other) or other[i] != base[i]:
                return base[:i]
    return base


def _longest_common_suffix(strings: list[str]) -> str:
    reversed_lcp = _longest_common_prefix([s[::-1] for s in strings])
    return reversed_lcp[::-1]


def _extract_system_block(prompt: str) -> str:
    start = prompt.find("<system>")
    end = prompt.find("</system>")
    if start < 0 or end < 0:
        return ""
    return prompt[start : end + len("</system>")]
