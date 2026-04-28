"""Regression tests that lock the prompt-unification invariants.

Target design (matches the active workplan):

* Cells A and B-BOSS receive the same input-prompt block:
  ``inputs/user.j2`` + ``inputs/cve.j2`` + ``inputs/control.j2``.
* Only the system prompt differs: A gets ``system/single_agent.j2``
  (a preamble + ``{% include %}`` of the three manager personas),
  B-BOSS gets the current BOSS persona.
* The gold patch and candidate-fix content NEVER appear in any rendered
  prompt.
* ``prompts/domains/secbench/briefing.md`` is retired once W3 lands.

Each assertion that is not yet satisfiable on the current branch is marked
``xfail(strict=True)``. When the corresponding workstream lands and the
test starts passing, pytest reports an XPASS error so the next contributor
remembers to remove the marker rather than letting it rot.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from core.application.run_invariants import build_task_prompt
from core.application.services import PromptBuilder
from core.domain.values.limits import HierarchyLimits
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import CVEInstance, SecBenchPromptStrategy


REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts"
BRIEFING_PATH = PROMPTS_DIR / "domains" / "secbench" / "briefing.md"


# Marker tokens chosen so the test can grep raw rendered output and assert
# an exact-string presence/absence. Any leak of the corresponding CVE field
# leaves these tokens in the prompt and trips the test.
GOLD_PATCH_MARKER = "GOLD_PATCH_MARKER_DEADBEEF"
CANDIDATE_FIX_MARKER = "CANDIDATE_FIX_MARKER_FEEDFACE"
BUG_REPORT_MARKER = "BUG_REPORT_MARKER_BAADF00D"


def _make_cve_with_markers() -> CVEInstance:
    return CVEInstance(
        instance_id="demo.cve-9999-0001",
        repo="demo/project",
        project_name="demo",
        lang="c",
        work_dir="/src/demo",
        sanitizer="address",
        bug_description="Heap overflow in demo parser at src/demo.c:42.",
        base_commit="a" * 40,
        build_sh="#!/bin/bash\nmake",
        secb_sh="#!/bin/bash\nrun",
        dockerfile="FROM scratch",
        patch=(
            "diff --git a/src/demo.c b/src/demo.c\n"
            "--- a/src/demo.c\n"
            "+++ b/src/demo.c\n"
            "@@ -1 +1 @@\n"
            f"-old\n+{GOLD_PATCH_MARKER}\n"
        ),
        candidate_fixes=f"alternative-fix: {CANDIDATE_FIX_MARKER}",
        sanitizer_report="==1==ERROR: AddressSanitizer: heap-buffer-overflow",
        bug_report=f"Upstream issue narrative: {BUG_REPORT_MARKER}",
    )


def _make_builder() -> PromptBuilder:
    return PromptBuilder(
        template_dir=PROMPTS_DIR,
        default_tool="claude_code",
        strategy=SecBenchPromptStrategy(),
    )


def _render_cell_a_prompt(cve: CVEInstance) -> str:
    """Render the prompt that Cell A's claude_code process currently receives.

    Mirrors ``bootstrap/composition._make_flat_invariant_builder``: the
    briefing markdown + a JSON dump of the CVE fixture. TODO(W3): switch
    to the unified ``inputs/*`` templates and remove the briefing+JSON
    path entirely.
    """
    return build_task_prompt(
        briefing_path=BRIEFING_PATH,
        cve_context=cve.to_template_context(),
        task=cve.instance_id,
    ).rendered_prompt


def _render_b_boss_prompt(cve: CVEInstance) -> str:
    builder = _make_builder()
    root_id = uuid4()
    limits = HierarchyLimits.create_root(
        root_id=root_id,
        max_depth=3,
        max_children_per_node=4,
        max_retries=2,
        max_total_agents=20,
        domain_context=cve,
    )
    return builder.build_boss_delegation_prompt(
        task_description=cve.instance_id,
        agent_id=root_id,
        domain_context=cve,
        hierarchy_limits=limits,
    )


_PHASE_BRACKET = {
    "builder": "[Builder]",
    "exploiter": "[Exploiter]",
    "fixer": "[Fixer]",
    "reporter": "[Reporter]",
}


def _boss_ancestor(cve: CVEInstance) -> Ancestor:
    return Ancestor(
        agent_id=str(uuid4()),
        role="boss",
        task_summary=cve.instance_id,
    )


def _phase_briefing(cve: CVEInstance, phase: str) -> Briefing:
    """Single-ancestor briefing so the per-phase Jinja branch renders.

    `SecBenchPromptStrategy.extend_manager_prompt` only renders the
    `manager/<phase>.j2` template when the manager is a direct child of
    BOSS — i.e., `len(briefing.ancestry) == 1`. Without this fixture the
    per-phase content is silently skipped and the per-phase manager
    leak surface goes untested.
    """
    bracket = _PHASE_BRACKET[phase]
    return Briefing(
        parent_task=f"{bracket} Phase task for {cve.instance_id}",
        parent_role="boss",
        ancestry=(_boss_ancestor(cve),),
    )


def _render_b_manager_prompt(cve: CVEInstance, phase: str) -> str:
    builder = _make_builder()
    bracket = _PHASE_BRACKET[phase]
    return builder.build_manager_decomposition_prompt(
        task_description=f"{bracket} Phase task for {cve.instance_id}",
        agent_id=uuid4(),
        domain_context=cve,
        briefing=_phase_briefing(cve, phase),
    )


def _render_b_worker_prompt(cve: CVEInstance, phase: str) -> str:
    builder = _make_builder()
    bracket = _PHASE_BRACKET[phase]
    return builder.build_worker_prompt(
        task_description=f"{bracket} Worker task for {cve.instance_id}",
        agent_id=uuid4(),
        domain_context=cve,
        briefing=_phase_briefing(cve, phase),
    )


def _all_rendered_prompts(cve: CVEInstance) -> dict[str, str]:
    prompts: dict[str, str] = {
        "A_flat": _render_cell_a_prompt(cve),
        "B_boss": _render_b_boss_prompt(cve),
    }
    for phase in ("builder", "exploiter", "fixer"):
        prompts[f"B_manager_{phase}"] = _render_b_manager_prompt(cve, phase)
        prompts[f"B_worker_{phase}"] = _render_b_worker_prompt(cve, phase)
    return prompts


# ---------------------------------------------------------------------------
# Sanity — confirm the markers actually travel through the fixture so a
# future change that vacuously hides them (e.g., a base64-encoded patch
# field) breaks this test instead of silently passing the leak tests.
# ---------------------------------------------------------------------------


def test_marker_fixture_carries_substrings_into_cve_instance() -> None:
    cve = _make_cve_with_markers()
    assert GOLD_PATCH_MARKER in cve.patch
    assert CANDIDATE_FIX_MARKER in cve.candidate_fixes
    assert BUG_REPORT_MARKER in cve.bug_report
    dump = cve.model_dump()
    assert GOLD_PATCH_MARKER in dump["patch"]
    assert CANDIDATE_FIX_MARKER in dump["candidate_fixes"]
    assert BUG_REPORT_MARKER in dump["bug_report"]


# ---------------------------------------------------------------------------
# 5.2 — gold patch must never appear in any rendered prompt (W1 target)
# ---------------------------------------------------------------------------


def test_no_gold_patch_marker_anywhere() -> None:
    cve = _make_cve_with_markers()
    rendered = _all_rendered_prompts(cve)
    leaks = {name: prompt for name, prompt in rendered.items() if GOLD_PATCH_MARKER in prompt}
    assert not leaks, f"Gold patch leaked into prompts: {sorted(leaks)}"


def test_no_candidate_fix_marker_anywhere() -> None:
    cve = _make_cve_with_markers()
    rendered = _all_rendered_prompts(cve)
    leaks = {name: prompt for name, prompt in rendered.items() if CANDIDATE_FIX_MARKER in prompt}
    assert not leaks, f"candidate_fixes leaked into prompts: {sorted(leaks)}"


def test_no_diff_markers_in_any_prompt() -> None:
    cve = _make_cve_with_markers()
    rendered = _all_rendered_prompts(cve)
    # JSON encoding turns real newlines into the literal `\n` escape, so
    # line-anchored regexes don't fire. Search anywhere — we want zero
    # occurrences of these substrings regardless of how they're embedded.
    needles = ("diff --git ", "+++ b/")
    leaks: dict[str, list[str]] = {}
    for name, prompt in rendered.items():
        hits = [n for n in needles if n in prompt]
        if hits:
            leaks[name] = hits
    assert not leaks, f"Unified-diff markers found in prompts: {leaks}"


# ---------------------------------------------------------------------------
# Bug report content stays — explicit positive assertion to guard against
# over-aggressive filtering during W2.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "bug_report is not in _CVE_DISPLAY_FIELDS yet. W2 adds it to the "
        "allowlist so workers see the upstream issue narrative."
    ),
    strict=True,
)
def test_bug_report_is_present_in_b_boss_prompt() -> None:
    cve = _make_cve_with_markers()
    boss_prompt = _render_b_boss_prompt(cve)
    assert BUG_REPORT_MARKER in boss_prompt, (
        "bug_report content must be carried into the BOSS prompt — the user "
        "explicitly opted to keep it; do not strip it during W2."
    )


# ---------------------------------------------------------------------------
# 5.1 — the input block (user + cve + control) is identical for A and B-BOSS
#       once W2/W3/W4 land. Currently A's path is briefing.md+JSON dump and
#       B's path is the Jinja chain — they cannot match yet.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Until W2 lands `prompts/inputs/{user,cve,control}.j2` and W3/W4 "
        "rewire both A and B-BOSS through them, A and B-BOSS produce "
        "different input blocks."
    ),
    strict=True,
)
def test_input_block_identical_for_a_and_boss() -> None:
    cve = _make_cve_with_markers()
    builder = _make_builder()

    # The canonical input block once W2 lands. The chain factory is the
    # same one PromptBuilder uses internally for its strategy hooks.
    input_block = (
        builder.chain()
        .render("inputs/user.j2", **cve.to_template_context())
        .render("inputs/cve.j2", **cve.to_template_context())
        .render("inputs/control.j2", **cve.to_template_context())
        .build()
    )

    a_prompt = _render_cell_a_prompt(cve)
    b_boss_prompt = _render_b_boss_prompt(cve)

    assert input_block in a_prompt, (
        "Cell A's prompt is missing the canonical input block "
        "(inputs/user + inputs/cve + inputs/control)."
    )
    assert input_block in b_boss_prompt, "B-BOSS prompt is missing the canonical input block."


# ---------------------------------------------------------------------------
# 5.5 — anti-cheat rules are present in every cell's BOSS-level prompt.
#       Today they live only in briefing.md (Cell A), so B-BOSS fails this.
#       Closes once W4 injects inputs/control.j2 into the BOSS chain.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Until W4 wires inputs/control.j2 into the BOSS chain, B-BOSS "
        "never sees the anti-cheat rules."
    ),
    strict=True,
)
def test_anti_cheat_rules_present_in_b_boss_prompt() -> None:
    cve = _make_cve_with_markers()
    boss_prompt = _render_b_boss_prompt(cve)
    canonical_phrases = (
        "git log --all",
        "git checkout",
        "github.com",
    )
    missing = [phrase for phrase in canonical_phrases if phrase not in boss_prompt]
    assert not missing, f"B-BOSS prompt is missing anti-cheat phrases: {missing}"


# ---------------------------------------------------------------------------
# 5.3 — A's `system/single_agent.j2` locks to the union of the three
#       manager personas. Render the file, strip a known preamble marker,
#       and assert the remainder equals the concatenated manager templates.
# ---------------------------------------------------------------------------


PREAMBLE_END_MARKER = '"execute this step yourself"'


@pytest.mark.xfail(
    reason="prompts/system/single_agent.j2 does not exist yet (W3).",
    strict=True,
)
def test_single_agent_persona_locks_to_three_managers() -> None:
    cve = _make_cve_with_markers()
    builder = _make_builder()
    ctx = cve.to_template_context()

    single_agent = builder.chain().render("system/single_agent.j2", **ctx).build()

    # Strip the preamble — everything up to and including the trailing
    # marker phrase. What remains should be the three manager templates
    # rendered with the same context, in declared order.
    marker_idx = single_agent.find(PREAMBLE_END_MARKER)
    assert marker_idx != -1, (
        "single_agent.j2 preamble must end with the canonical marker "
        f"phrase {PREAMBLE_END_MARKER!r}; got prompt:\n{single_agent[:400]}"
    )
    after_preamble = single_agent[marker_idx + len(PREAMBLE_END_MARKER) :]

    expected = (
        builder.chain()
        .render("domains/secbench/manager/builder.j2", **ctx)
        .render("domains/secbench/manager/exploiter.j2", **ctx)
        .render("domains/secbench/manager/fixer.j2", **ctx)
        .build()
    )

    # Allow whitespace differences at section boundaries (Jinja
    # trim_blocks/lstrip_blocks setting may insert a leading newline);
    # the substantive content must match.
    assert after_preamble.strip() == expected.strip(), (
        "single_agent.j2 must be exactly preamble + three manager personas. Drift detected."
    )


# ---------------------------------------------------------------------------
# 5.4 — briefing.md is retired and no Python references it.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="briefing.md still exists and is referenced; W3 retires it.",
    strict=True,
)
def test_briefing_md_is_removed() -> None:
    assert not BRIEFING_PATH.exists(), (
        f"briefing.md should be retired by W3; still present at {BRIEFING_PATH}"
    )


@pytest.mark.xfail(
    reason=(
        "composition + run_invariants still reference the briefing path; W3 deletes the helpers."
    ),
    strict=True,
)
def test_no_python_references_to_briefing_helpers() -> None:
    # Word-boundary regex avoids false positives from substrings matching
    # unrelated identifiers. ``briefing_path`` is currently a parameter
    # name on ``build_task_prompt`` only; if a future, unrelated function
    # adopts the same identifier we'd want to know — but we accept that
    # signal rather than masking it with looser matching.
    forbidden_patterns = [
        re.compile(rf"\b{re.escape(token)}\b")
        for token in ("_resolve_briefing_path", "build_task_prompt", "briefing_path")
    ]
    skip_dirs = {".venv", "venv", "build", ".worktrees", ".mypy_cache"}
    offenders: list[tuple[Path, str]] = []
    for py_file in REPO_ROOT.rglob("*.py"):
        if py_file == Path(__file__):
            continue
        rel = py_file.relative_to(REPO_ROOT)
        if rel.parts and rel.parts[0] in skip_dirs:
            continue
        try:
            content = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pat in forbidden_patterns:
            if pat.search(content):
                offenders.append((rel, pat.pattern))
    assert not offenders, "Python references to retired briefing helpers found:\n" + "\n".join(
        f"  {p}: {tok}" for p, tok in offenders
    )
