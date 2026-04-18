"""Tests for the inject_tool_guidance_always flag on SecurityDomainPlugin."""

from collections.abc import Callable
from uuid import uuid4

from core.application.services import PromptBuilder
from core.domain.values.node_message import Ancestor, Briefing
from plugins.security import SecBenchPromptStrategy, SecurityDomainPlugin
from plugins.security.tests.test_prompt_building import PROMPTS_DIR, make_test_cve_instance


def _make_briefing_without_phase_keywords() -> Briefing:
    """Return a briefing whose ancestry contains no SEC-bench phase keywords."""
    return Briefing(
        parent_task="Top-level generic task",
        parent_role="boss",
        ancestry=(
            Ancestor(
                agent_id=str(uuid4()),
                role="boss",
                task_summary="Investigate the provided workload.",
            ),
        ),
    )


def _make_briefing_exploiter_phase() -> Briefing:
    """Return a briefing whose ancestry triggers the exploiter phase."""
    return Briefing(
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


def _chain_factory() -> Callable[[], object]:
    # The PromptBuilder owns the Jinja environment, so reuse its chain
    # factory for realistic rendering behavior (same as production wiring).
    builder = PromptBuilder(
        template_dir=PROMPTS_DIR,
        default_tool="claude_code",
        strategy=SecBenchPromptStrategy(),
    )
    return builder.chain


def test_enrich_prompt_injects_tools_when_always_flag_set_even_without_phase_ancestry() -> None:
    """Flag=True injects Valgrind guidance even when ancestry lacks SEC-bench phase keywords."""
    # Given: a plugin with inject_tool_guidance_always=True and a generic briefing
    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind"],
        inject_tool_guidance_always=True,
    )
    cve = make_test_cve_instance()
    briefing = _make_briefing_without_phase_keywords()

    # When: enrich_prompt is called with the no-phase briefing
    enriched = plugin.enrich_prompt(
        "base prompt",
        domain_context=cve,
        briefing=briefing,
        chain_factory=_chain_factory(),
    )

    # Then: Valgrind guidance is present and the original prompt is preserved
    assert enriched.startswith("base prompt")
    assert "valgrind" in enriched.lower()
    assert "<security_tools>" in enriched


def test_enrich_prompt_omits_tools_when_always_flag_false_and_no_phase_ancestry() -> None:
    """Flag=False preserves legacy behavior: no phase in ancestry means no injection."""
    # Given: a plugin with inject_tool_guidance_always=False and a generic briefing
    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind"],
        inject_tool_guidance_always=False,
    )
    cve = make_test_cve_instance()
    briefing = _make_briefing_without_phase_keywords()

    # When: enrich_prompt is called with the no-phase briefing
    enriched = plugin.enrich_prompt(
        "base prompt",
        domain_context=cve,
        briefing=briefing,
        chain_factory=_chain_factory(),
    )

    # Then: the prompt is returned unchanged (no tool guidance injected)
    assert enriched == "base prompt"


def test_enrich_prompt_injects_tools_when_phase_detected_regardless_of_flag() -> None:
    """Flag does not affect the detected-phase path: exploiter ancestry yields Valgrind."""
    # Given: a plugin with inject_tool_guidance_always=False and exploiter ancestry
    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind"],
        inject_tool_guidance_always=False,
    )
    cve = make_test_cve_instance()
    briefing = _make_briefing_exploiter_phase()

    # When: enrich_prompt is called with a phase-detectable briefing
    enriched = plugin.enrich_prompt(
        "base prompt",
        domain_context=cve,
        briefing=briefing,
        chain_factory=_chain_factory(),
    )

    # Then: Valgrind guidance is injected via the normal phase-detected flow
    assert enriched.startswith("base prompt")
    assert "valgrind" in enriched.lower()


def test_enrich_prompt_with_flag_true_renders_all_enabled_tools_via_fallback_phase() -> None:
    """Fallback phase must advertise every enabled tool so Valgrind+KLEE always co-appear."""
    # Given: a plugin with both Valgrind and KLEE enabled and a no-phase briefing
    plugin = SecurityDomainPlugin(
        enabled_tools=["valgrind", "klee"],
        inject_tool_guidance_always=True,
    )
    cve = make_test_cve_instance()
    briefing = _make_briefing_without_phase_keywords()

    # When: enrich_prompt falls back to the union-yielding phase
    enriched = plugin.enrich_prompt(
        "base prompt",
        domain_context=cve,
        briefing=briefing,
        chain_factory=_chain_factory(),
    )

    # Then: both enabled tools appear in the enriched prompt (spec-level invariant)
    lowered = enriched.lower()
    assert "valgrind" in lowered
    assert "klee" in lowered
