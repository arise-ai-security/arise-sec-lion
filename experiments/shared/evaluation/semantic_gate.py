"""Three-call blinded semantic/root-cause gate and human-audit routing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from experiments.shared.evaluation.judge import DEFAULT_JUDGE_MODEL, LLMJudge


SEMANTIC_RUBRIC: dict[str, str] = {
    "root_cause_correct": "bool",
    "patch_addresses_root_cause": "bool",
    "over_broad_suppression": "bool",
    "evidence_consistent": "bool",
    "regression_risk_acceptable": "bool",
    "verdict": "bool",
    "reason": "str",
}
_BLINDED_FIELDS = frozenset({"cell", "model", "cost", "topology", "treatment_version"})


def build_blinded_semantic_prompt(evidence: dict[str, Any]) -> str:
    """Render the frozen rubric without arm, model, cost, or topology identity."""
    leaked = _BLINDED_FIELDS.intersection(evidence)
    if leaked:
        raise ValueError(f"semantic evidence contains blinded fields: {sorted(leaked)}")
    rubric = {
        "root_cause_correct": "Does the analysis identify the verified causal defect?",
        "patch_addresses_root_cause": "Does the patch repair that defect at the correct layer?",
        "over_broad_suppression": "Does the patch suppress detection or valid behavior broadly?",
        "evidence_consistent": "Are claims consistent with host execution evidence?",
        "regression_risk_acceptable": "Is regression risk acceptable for the demonstrated scope?",
        "verdict": "True only when every required semantic condition passes.",
        "reason": "Concise evidence-grounded rationale.",
    }
    return (
        "Evaluate the blinded security fix using this frozen rubric. Return only the "
        "structured schema.\nRUBRIC:\n"
        + json.dumps(rubric, sort_keys=True)
        + "\nEVIDENCE:\n"
        + json.dumps(evidence, sort_keys=True)
    )


class SemanticJudge(Protocol):
    def __call__(self, built: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SemanticGateResult:
    provisional_verdict: bool
    unanimous: bool
    judge_error: bool
    human_audit_required: bool
    audit_reason: str | None
    judge_model: str
    calls: tuple[dict[str, Any], ...]


def adjudicate_human_reviews(
    first: bool,
    second: bool,
    *,
    third: bool | None = None,
) -> bool:
    """Two reviewers decide; disagreement requires a third reviewer."""
    if first == second:
        return first
    if third is None:
        raise ValueError("third reviewer required when the first two disagree")
    return (int(first) + int(second) + int(third)) >= 2


def _default_judge_factory() -> SemanticJudge:
    """Production default: a fresh judge pinned to the reproducible snapshot."""
    return LLMJudge(model=DEFAULT_JUDGE_MODEL)


def _enforce_pinned_model(judges: tuple[SemanticJudge, ...]) -> str:
    """Reject any judge bound to a model other than the pinned snapshot.

    Bare test doubles that advertise no ``model`` pass through the injection
    seam; any judge that does advertise a model must advertise the pinned one.
    """
    for judge in judges:
        model = getattr(judge, "model", None)
        if model is not None and model != DEFAULT_JUDGE_MODEL:
            raise ValueError(
                f"semantic gate requires pinned judge model {DEFAULT_JUDGE_MODEL!r}, "
                f"got {model!r}"
            )
    return DEFAULT_JUDGE_MODEL


def evaluate_semantic_gate(
    *,
    task_id: str,
    evidence: dict[str, Any],
    judge_factory: Callable[[], SemanticJudge] | None = None,
    audit_rate: float = 0.10,
) -> SemanticGateResult:
    """Run three independent blinded calls and route non-unanimous cases.

    The gate blinds ``evidence`` itself via :func:`build_blinded_semantic_prompt`
    (the caller can never supply raw prompt text) and pins every judge to
    :data:`DEFAULT_JUDGE_MODEL`, failing closed on either violation.
    """
    prompt = build_blinded_semantic_prompt(evidence)
    built = {"prompt": prompt, "schema": SEMANTIC_RUBRIC, "excerpts": {}}
    judges = tuple((judge_factory or _default_judge_factory)() for _ in range(3))
    judge_model = _enforce_pinned_model(judges)
    calls = tuple(judge(built) for judge in judges)
    judge_error = any(bool(call.get("_error")) for call in calls)
    verdicts = [bool(call.get("verdict", False)) for call in calls]
    unanimous = len(set(verdicts)) == 1 and not judge_error
    provisional = verdicts.count(True) >= 2 if not judge_error else False
    sampled = _stable_audit_sample(task_id, audit_rate)
    human_required = judge_error or not unanimous or sampled
    if judge_error:
        reason = "judge_error"
    elif not unanimous:
        reason = "non_unanimous_2_to_1"
    elif sampled:
        reason = "stratified_unanimous_audit_sample"
    else:
        reason = None
    return SemanticGateResult(
        provisional_verdict=provisional,
        unanimous=unanimous,
        judge_error=judge_error,
        human_audit_required=human_required,
        audit_reason=reason,
        judge_model=judge_model,
        calls=calls,
    )


def _stable_audit_sample(task_id: str, audit_rate: float) -> bool:
    if not 0.0 <= audit_rate <= 1.0:
        raise ValueError("audit_rate must be between 0 and 1")
    bucket = int.from_bytes(hashlib.sha256(task_id.encode("utf-8")).digest()[:8], "big")
    return bucket / (2**64 - 1) < audit_rate


def apply_human_override(result: SemanticGateResult, human_verdict: bool) -> bool:
    """Human adjudication is authoritative for audited cases."""
    if not result.human_audit_required:
        raise ValueError("human override is only valid for an audited case")
    return human_verdict


def unanimous_audit_requires_full_review(*, audited: int, disagreements: int) -> bool:
    """Expand review when audited unanimous disagreement exceeds five percent."""
    if audited <= 0:
        return False
    return disagreements / audited > 0.05
