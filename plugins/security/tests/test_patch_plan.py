"""Tests for validated literal patch-plan execution."""

import hashlib

import pytest
from pydantic import ValidationError

from plugins.security.patch_plan import PatchApplier, PatchOperation, PatchPlan, PatchPlanValidator


def _plan(source_file, **updates) -> PatchPlan:
    content = source_file.read_bytes()
    values = {
        "evidence_references": ("event:root-cause",),
        "target_file": "/src/project/vulnerable.c",
        "target_symbol": "parse",
        "base_sha256": hashlib.sha256(content).hexdigest(),
        "operations": (
            PatchOperation(kind="replace", anchor="unsafe();", replacement="safe();"),
        ),
        "allowed_paths": ("/src/project",),
        "forbidden_paths": ("/testcase",),
        "required_postconditions": ("sanitizer finding absent",),
        "validation_commands": ("secb patch", "secb build", "secb repro"),
    }
    values.update(updates)
    return PatchPlan(**values)


def test_patch_applier_applies_validated_literal_edit(tmp_path) -> None:
    """A valid hash, anchor, allowlist, and evidence set permits the exact edit."""

    # Given: A source tree and a fully valid patch plan
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    plan = _plan(source)
    applier = PatchApplier(PatchPlanValidator(tmp_path, {"event:root-cause"}))

    # When: The literal applier runs
    result = applier.apply(plan)

    # Then: The exact replacement is written and the plan hash is frozen
    assert result.status == "APPLIED"
    assert result.plan_sha256 == plan.sha256
    assert source.read_text(encoding="utf-8") == "void parse(void) { safe(); }\n"


def test_patch_plan_contains_only_solver_authored_fields(tmp_path) -> None:
    """Host replay identity stays private and outside the solver-authored plan."""

    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")

    plan = _plan(source)

    assert "pre_patch_exploit_identity" not in plan.model_dump()


@pytest.mark.parametrize("field", ("pre_patch_exploit_identity", "arbitrary_key"))
def test_patch_plan_rejects_unknown_top_level_fields(tmp_path, field: str) -> None:
    # Given: A valid plan extended with a non-schema field
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    payload = _plan(source).model_dump()
    payload[field] = "must-not-be-accepted"

    # When/Then: Strict schema validation rejects the field instead of discarding it
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PatchPlan.model_validate(payload)


def test_patch_plan_rejects_unknown_operation_fields(tmp_path) -> None:
    # Given: A valid plan whose nested operation carries an arbitrary field
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    payload = _plan(source).model_dump()
    operation = dict(payload["operations"][0])
    operation["arbitrary_key"] = "must-not-be-accepted"
    payload["operations"] = (operation,)

    # When/Then: The nested schema rejects unknown fields too
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PatchPlan.model_validate(payload)


def test_patch_applier_makes_no_edit_when_hash_mismatches(tmp_path) -> None:
    """A stale base hash blocks before writing."""

    # Given: A plan whose base hash no longer matches the source
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    plan = _plan(source, base_sha256="0" * 64)
    before = source.read_bytes()

    # When: The literal applier validates the plan
    result = PatchApplier(PatchPlanValidator(tmp_path, {"event:root-cause"})).apply(plan)

    # Then: It reports PATCH_PLAN_BLOCKED and preserves every byte
    assert result.status == "PATCH_PLAN_BLOCKED"
    assert "hash" in result.reason
    assert source.read_bytes() == before


def test_patch_applier_makes_no_edit_when_anchor_is_ambiguous(tmp_path) -> None:
    """A non-unique anchor blocks before writing."""

    # Given: Source containing the planned anchor twice
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("unsafe();\nunsafe();\n", encoding="utf-8")
    plan = _plan(source)
    before = source.read_bytes()

    # When: The literal applier validates the plan
    result = PatchApplier(PatchPlanValidator(tmp_path, {"event:root-cause"})).apply(plan)

    # Then: The ambiguity blocks and no edit occurs
    assert result.status == "PATCH_PLAN_BLOCKED"
    assert "anchor occurrence" in result.reason
    assert source.read_bytes() == before


def test_patch_applier_honors_expected_occurrences_for_multi_site_edit(tmp_path) -> None:
    """A plan may target N identical anchors; validation honors the declared count."""

    # Given: source with the anchor exactly twice and a plan expecting two occurrences
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("unsafe();\nunsafe();\n", encoding="utf-8")
    plan = _plan(
        source,
        operations=(
            PatchOperation(
                kind="replace",
                anchor="unsafe();",
                replacement="safe();",
                expected_occurrences=2,
            ),
        ),
    )

    # When: the literal applier runs
    result = PatchApplier(PatchPlanValidator(tmp_path, {"event:root-cause"})).apply(plan)

    # Then: the plan is approved and both matched sites are transformed
    assert result.status == "APPLIED"
    assert source.read_text(encoding="utf-8") == "safe();\nsafe();\n"


def test_patch_applier_makes_no_edit_for_forbidden_or_unresolved_input(tmp_path) -> None:
    """Path and provenance failures are fail-closed."""

    # Given: A source file whose plan forbids its own target and cites missing evidence
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("unsafe();\n", encoding="utf-8")
    forbidden = _plan(source, forbidden_paths=("/src/project",))
    unresolved = _plan(source, evidence_references=("event:missing",))
    validator = PatchPlanValidator(tmp_path, {"event:root-cause"})
    before = source.read_bytes()

    # When: Both plans are offered to the literal applier
    forbidden_result = PatchApplier(validator).apply(forbidden)
    unresolved_result = PatchApplier(validator).apply(unresolved)

    # Then: Both block without changing the source
    assert forbidden_result.status == "PATCH_PLAN_BLOCKED"
    assert unresolved_result.status == "PATCH_PLAN_BLOCKED"
    assert source.read_bytes() == before


def test_patch_applier_renders_diff_without_mutating_source(tmp_path) -> None:
    """Rendering prepares the validator input while preserving the fresh source base."""

    # Given: A valid literal plan over an unchanged source tree
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    source.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    plan = _plan(source)
    before = source.read_bytes()

    # When: The plan is rendered for independent patch validation
    result = PatchApplier(PatchPlanValidator(tmp_path, {"event:root-cause"})).render(plan)

    # Then: The diff contains the literal replacement and source remains unchanged
    assert result.status == "RENDERED"
    assert "+void parse(void) { safe(); }" in result.diff
    assert source.read_bytes() == before


def test_patch_applier_preserves_crlf_and_decomposed_utf8_bytes(tmp_path) -> None:
    """Rendering and applying preserve every byte outside the literal replacement."""

    # Given: A CRLF source whose UTF-8 comment uses a decomposed accent
    source = tmp_path / "project" / "vulnerable.c"
    source.parent.mkdir()
    original = b"// Cafe\xcc\x81\r\nvoid parse(void) { unsafe(); }\r\n"
    patched = b"// Cafe\xcc\x81\r\nvoid parse(void) { safe(); }\r\n"
    source.write_bytes(original)
    plan = _plan(source)
    applier = PatchApplier(PatchPlanValidator(tmp_path, {"event:root-cause"}))

    # When: The Host renders and then applies the exact plan
    rendered = applier.render(plan)
    applied = applier.apply(plan)

    # Then: The render round-trips the exact preimage and patched bytes
    assert rendered.status == "RENDERED"
    assert rendered.original_content is not None
    assert rendered.patched_content is not None
    assert rendered.original_content.encode("utf-8") == original
    assert rendered.patched_content.encode("utf-8") == patched
    assert hashlib.sha256(rendered.original_content.encode("utf-8")).hexdigest() == plan.base_sha256
    assert hashlib.sha256(rendered.patched_content.encode("utf-8")).hexdigest() == hashlib.sha256(
        patched
    ).hexdigest()

    # And: Applying writes the exact bytes without newline or Unicode normalization
    assert applied.status == "APPLIED"
    assert source.read_bytes() == patched
