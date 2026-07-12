"""Tests for three-call semantic judgement and audit routing."""

import pytest

from experiments.shared.evaluation.judge import DEFAULT_JUDGE_MODEL
from experiments.shared.evaluation.semantic_gate import (
    _default_judge_factory,
    adjudicate_human_reviews,
    apply_human_override,
    build_blinded_semantic_prompt,
    evaluate_semantic_gate,
    unanimous_audit_requires_full_review,
)


class _Judge:
    def __init__(self, verdict):
        self._verdict = verdict

    def __call__(self, built):
        del built
        if self._verdict == "error":
            return {"verdict": False, "_error": True}
        return {"verdict": self._verdict, "reason": "test"}


def _factory(verdicts):
    pending = iter(verdicts)
    return lambda: _Judge(next(pending))


class _RecordingJudge:
    """Pinned-model fake that captures every ``built`` payload it receives."""

    def __init__(self, captured, *, model=DEFAULT_JUDGE_MODEL, verdict=True):
        self.model = model
        self._captured = captured
        self._verdict = verdict

    def __call__(self, built):
        self._captured.append(built)
        return {"verdict": self._verdict, "reason": "test"}


def _recording_factory(captured, *, model=DEFAULT_JUDGE_MODEL, verdict=True):
    return lambda: _RecordingJudge(captured, model=model, verdict=verdict)


def test_unanimous_three_call_result_is_provisionally_accepted() -> None:
    # Given: Three independent positive judges outside the audit sample
    factory = _factory([True, True, True])

    # When: The semantic gate runs
    result = evaluate_semantic_gate(
        task_id="unanimous",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
        audit_rate=0,
    )

    # Then: It accepts provisionally without human review
    assert result.provisional_verdict
    assert result.unanimous
    assert not result.human_audit_required


def test_two_to_one_result_enters_human_adjudication() -> None:
    # Given: A split panel
    factory = _factory([True, False, True])

    # When: The semantic gate runs and a human adjudicates
    result = evaluate_semantic_gate(
        task_id="split",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
        audit_rate=0,
    )
    final = apply_human_override(result, human_verdict=False)

    # Then: Human review is mandatory and overrides the provisional majority
    assert result.provisional_verdict
    assert result.human_audit_required
    assert not final


def test_judge_error_fails_closed_and_requires_human_review() -> None:
    # Given: One failed judge call
    factory = _factory([True, "error", True])

    # When: The semantic gate runs
    result = evaluate_semantic_gate(
        task_id="error",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
        audit_rate=0,
    )

    # Then: It fails closed and queues adjudication
    assert not result.provisional_verdict
    assert result.judge_error
    assert result.human_audit_required


def test_unanimous_audit_disagreement_above_five_percent_expands_review() -> None:
    # Given/When/Then: Exactly 5% does not expand; above 5% does
    assert not unanimous_audit_requires_full_review(audited=20, disagreements=1)
    assert unanimous_audit_requires_full_review(audited=19, disagreements=1)


def test_blinded_prompt_rejects_arm_identity() -> None:
    # Given/When/Then: Cell, model, cost, topology, and treatment cannot enter judge input
    with pytest.raises(ValueError, match="blinded fields"):
        build_blinded_semantic_prompt({"cell": "B4", "root_cause": "evidence"})


def test_human_disagreement_requires_third_reviewer() -> None:
    # Given/When/Then: Two-reviewer disagreement cannot be resolved without reviewer three
    with pytest.raises(ValueError, match="third reviewer"):
        adjudicate_human_reviews(True, False)
    assert adjudicate_human_reviews(True, False, third=False) is False


def test_gate_rejects_evidence_carrying_arm_identity() -> None:
    # Given: Evidence that leaks cell / model / cost / topology identity
    captured: list = []
    factory = _recording_factory(captured)

    # When/Then: The gate blinds internally and fails closed before any judge runs
    for leaked in ("cell", "model", "cost", "topology", "treatment_version"):
        with pytest.raises(ValueError, match="blinded fields"):
            evaluate_semantic_gate(
                task_id="leak",
                evidence={leaked: "B4", "root_cause": "rc"},
                judge_factory=factory,
                audit_rate=0,
            )
    assert captured == []


def test_gate_builds_blinded_prompt_internally() -> None:
    # Given: A clean structured record (the caller cannot supply raw prompt text)
    captured: list = []
    factory = _recording_factory(captured)
    evidence = {"root_cause": "rc", "patch": "p"}

    # When: The gate runs
    evaluate_semantic_gate(
        task_id="clean", evidence=evidence, judge_factory=factory, audit_rate=0
    )

    # Then: Each judge sees exactly the internally blinded prompt, nothing caller-authored
    assert len(captured) == 3
    expected_prompt = build_blinded_semantic_prompt(evidence)
    for built in captured:
        assert built["prompt"] == expected_prompt


def test_gate_binds_pinned_judge_model() -> None:
    # Given: A judge factory bound to the pinned reproducibility snapshot
    captured: list = []
    factory = _recording_factory(captured, model=DEFAULT_JUDGE_MODEL)

    # When: The gate runs
    result = evaluate_semantic_gate(
        task_id="pinned",
        evidence={"root_cause": "rc"},
        judge_factory=factory,
        audit_rate=0,
    )

    # Then: The result records the pinned model
    assert result.judge_model == DEFAULT_JUDGE_MODEL
    assert result.judge_model == "gpt-5.5-2026-04-23"


def test_gate_rejects_unpinned_judge_model() -> None:
    # Given: A judge factory bound to some other, unpinned model
    captured: list = []
    factory = _recording_factory(captured, model="gpt-4o-mini")

    # When/Then: The gate refuses to run against an unpinned judge
    with pytest.raises(ValueError, match="pinned judge model"):
        evaluate_semantic_gate(
            task_id="unpinned",
            evidence={"root_cause": "rc"},
            judge_factory=factory,
            audit_rate=0,
        )
    assert captured == []


def test_default_judge_factory_uses_pinned_model() -> None:
    # Given/When: The production default factory constructs a judge
    judge = _default_judge_factory()

    # Then: It is bound to the pinned snapshot, not a caller free-choice
    assert judge.model == DEFAULT_JUDGE_MODEL
