"""Tests for the zero-human heterogeneous semantic judge panel."""

from collections.abc import Callable

import pytest

import experiments.shared.evaluation.semantic_gate as semantic_gate
from experiments.shared.evaluation.semantic_gate import (
    SEMANTIC_JUDGE_MODELS,
    SEMANTIC_RUBRIC,
    _default_judge_factory,
    build_blinded_semantic_prompt,
    evaluate_semantic_gate,
)


class FakeJudge:
    def __init__(self, model: str, outcome: bool | str | Exception) -> None:
        self.model = model
        self._outcome = outcome

    def __call__(self, built: dict) -> dict:
        del built
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if self._outcome == "error":
            return {"verdict": False, "reason": "provider failure", "_error": True}
        if self._outcome == "missing":
            return {"reason": "schema violation"}
        if self._outcome == "contradictory":
            return {
                "root_cause_correct": False,
                "patch_addresses_root_cause": True,
                "over_broad_suppression": False,
                "evidence_consistent": True,
                "regression_risk_acceptable": True,
                "verdict": True,
                "reason": "contradictory",
            }
        return {
            "root_cause_correct": self._outcome,
            "patch_addresses_root_cause": self._outcome,
            "over_broad_suppression": not self._outcome,
            "evidence_consistent": self._outcome,
            "regression_risk_acceptable": self._outcome,
            "verdict": self._outcome,
            "reason": "test",
        }


def _factory(
    outcomes: tuple[bool | str | Exception, ...],
    requested: list[str] | None = None,
) -> Callable[[str], FakeJudge]:
    by_model = dict(zip(SEMANTIC_JUDGE_MODELS, outcomes, strict=True))

    def create(model: str) -> FakeJudge:
        if requested is not None:
            requested.append(model)
        return FakeJudge(model, by_model[model])

    return create


def test_two_positive_votes_accept_semantic_gate() -> None:
    # Given: Two positive provider votes and one negative vote
    factory = _factory((True, False, True))

    # When: The semantic panel aggregates the votes
    result = evaluate_semantic_gate(
        task_id="two-positive",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    # Then: The deterministic 2-of-3 rule accepts the fix
    assert result.available
    assert result.verdict
    assert result.valid_votes == 3
    assert result.positive_votes == 2
    assert not result.judge_error


def test_two_negative_votes_reject_semantic_gate() -> None:
    # Given: One positive provider vote and two negative votes
    factory = _factory((False, True, False))

    # When: The semantic panel aggregates the votes
    result = evaluate_semantic_gate(
        task_id="two-negative",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    # Then: The deterministic 2-of-3 rule rejects the fix
    assert result.available
    assert not result.verdict
    assert result.valid_votes == 3
    assert result.positive_votes == 1


def test_one_judge_error_makes_panel_unavailable() -> None:
    # Given: One provider error and two positive valid votes
    factory = _factory((True, "error", True))

    # When: The semantic panel aggregates only valid votes
    result = evaluate_semantic_gate(
        task_id="one-error",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    # Then: A missing heterogeneous seat makes confirmatory evaluation unavailable
    assert not result.available
    assert not result.verdict
    assert result.valid_votes == 2
    assert result.positive_votes == 2
    assert result.judge_error
    assert result.calls[1].verdict is None
    assert not result.calls[1].valid


def test_fewer_than_two_valid_votes_is_unavailable_and_fails_closed() -> None:
    # Given: Two provider errors and one positive valid vote
    factory = _factory(("error", True, RuntimeError("transport failed")))

    # When: The semantic panel attempts aggregation
    result = evaluate_semantic_gate(
        task_id="unavailable",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    # Then: Evaluation is unavailable and cannot pass
    assert not result.available
    assert not result.verdict
    assert result.valid_votes == 1
    assert result.positive_votes == 1
    assert result.judge_error


def test_two_valid_split_votes_are_unavailable() -> None:
    # Given: One positive vote, one negative vote, and one invalid response
    factory = _factory((True, False, "missing"))

    # When: The semantic panel aggregates the valid votes
    result = evaluate_semantic_gate(
        task_id="split-valid",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    # Then: Evaluation is unavailable because one seat is invalid
    assert not result.available
    assert not result.verdict
    assert result.valid_votes == 2
    assert result.positive_votes == 1
    assert result.judge_error


def test_logically_contradictory_judge_response_is_invalid() -> None:
    factory = _factory((True, "contradictory", True))

    result = evaluate_semantic_gate(
        task_id="contradictory",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    assert not result.available
    assert not result.verdict
    assert result.valid_votes == 2
    assert result.calls[1].verdict is None
    assert not result.calls[1].valid


def test_gate_requests_each_pinned_model_once_in_frozen_order() -> None:
    # Given: A factory recording every requested model
    requested: list[str] = []
    factory = _factory((True, True, True), requested)

    # When: The semantic panel runs
    result = evaluate_semantic_gate(
        task_id="models",
        evidence={"root_cause": "blinded evidence"},
        judge_factory=factory,
    )

    # Then: Each heterogeneous pinned model occupies exactly one seat
    assert tuple(requested) == SEMANTIC_JUDGE_MODELS
    assert result.models == SEMANTIC_JUDGE_MODELS


def test_result_persists_prompt_schema_models_and_raw_calls() -> None:
    # Given: Three judges returning distinct decisions
    evidence = {"root_cause": "verified defect", "patch": "bounded fix"}
    factory = _factory((True, False, True))

    # When: The semantic panel runs
    result = evaluate_semantic_gate(
        task_id="persisted",
        evidence=evidence,
        judge_factory=factory,
    )

    # Then: The complete frozen judge input and every raw response remain available
    assert result.task_id == "persisted"
    assert result.prompt == build_blinded_semantic_prompt(evidence)
    assert result.schema == SEMANTIC_RUBRIC
    assert result.models == SEMANTIC_JUDGE_MODELS
    assert tuple(call.model for call in result.calls) == SEMANTIC_JUDGE_MODELS
    assert tuple(call.response["verdict"] for call in result.calls) == (True, False, True)


@pytest.mark.parametrize(
    "field",
    ("cell", "model", "cost", "topology", "treatment_version"),
)
def test_blinded_prompt_rejects_nested_arm_identity_fields(field: str) -> None:
    # Given: A forbidden identity field nested below lists and mappings
    evidence = {"outer": [{"inner": {field: "B4"}}]}

    # When/Then: Recursive blinding rejects the packet before any judge runs
    with pytest.raises(ValueError, match=r"\$\.outer\[0\]\.inner"):
        build_blinded_semantic_prompt(evidence)


def test_gate_rejects_unpinned_model_returned_by_factory() -> None:
    # Given: A factory returning a judge whose advertised model differs from its seat
    def wrong_factory(model: str) -> FakeJudge:
        del model
        return FakeJudge("floating-latest", True)

    # When/Then: The gate rejects evaluator model drift
    with pytest.raises(ValueError, match="requires model"):
        evaluate_semantic_gate(
            task_id="unpinned",
            evidence={"root_cause": "verified"},
            judge_factory=wrong_factory,
        )


def test_default_factory_binds_each_requested_model() -> None:
    # Given/When: The production factory constructs each frozen judge seat
    judges = tuple(_default_judge_factory(model) for model in SEMANTIC_JUDGE_MODELS)

    # Then: No model is replaced by a global default or floating alias
    assert tuple(judge.model for judge in judges) == SEMANTIC_JUDGE_MODELS


def test_human_audit_api_is_removed() -> None:
    # Given/When/Then: The semantic module exposes no human review or override path
    assert not hasattr(semantic_gate, "adjudicate_human_reviews")
    assert not hasattr(semantic_gate, "apply_human_override")
    assert not hasattr(semantic_gate, "unanimous_audit_requires_full_review")


def test_human_audit_queue_module_is_removed() -> None:
    # Given/When/Then: No production human-audit queue or CLI remains
    import importlib.util

    assert importlib.util.find_spec("experiments.shared.evaluation.audit_queue") is None
