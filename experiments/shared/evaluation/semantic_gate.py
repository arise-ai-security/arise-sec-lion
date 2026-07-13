"""Zero-human heterogeneous semantic/root-cause judge panel."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from experiments.shared.evaluation.judge import LLMJudge


SEMANTIC_RUBRIC: dict[str, str] = {
    "root_cause_correct": "bool",
    "patch_addresses_root_cause": "bool",
    "over_broad_suppression": "bool",
    "evidence_consistent": "bool",
    "regression_risk_acceptable": "bool",
    "verdict": "bool",
    "reason": "str",
}
SEMANTIC_JUDGE_MODELS: tuple[str, ...] = (
    "claude-opus-4-8",
    "gpt-5.5-2026-04-23",
    # LiteLLM provider-qualified ID: bare `gemini-3.5-flash` routes to Vertex AI.
    "gemini/gemini-3.5-flash",
)
_BLINDED_FIELDS = frozenset({"cell", "model", "cost", "topology", "treatment_version"})


def _blinded_field_paths(value: Any, path: str = "$") -> tuple[str, ...]:
    leaked: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            field = str(key)
            nested_path = f"{path}.{field}"
            if field.casefold() in _BLINDED_FIELDS:
                leaked.append(nested_path)
            leaked.extend(_blinded_field_paths(nested, nested_path))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            leaked.extend(_blinded_field_paths(nested, f"{path}[{index}]"))
    return tuple(leaked)


def build_blinded_semantic_prompt(evidence: dict[str, Any]) -> str:
    """Render the frozen rubric without arm, model, cost, or topology identity."""
    leaked = _blinded_field_paths(evidence)
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
    model: str

    def __call__(self, built: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SemanticJudgeCall:
    """One pinned model's raw response and normalized vote validity."""

    model: str
    valid: bool
    verdict: bool | None
    response: dict[str, Any]
    started_at: str
    finished_at: str


@dataclass(frozen=True, slots=True)
class SemanticGateResult:
    """Persisted zero-human panel inputs, calls, and deterministic aggregation."""

    task_id: str
    verdict: bool
    available: bool
    valid_votes: int
    positive_votes: int
    judge_error: bool
    prompt: str
    schema: dict[str, str]
    models: tuple[str, ...]
    calls: tuple[SemanticJudgeCall, ...]


JudgeFactory = Callable[[str], SemanticJudge]


def _default_judge_factory(model: str) -> SemanticJudge:
    return LLMJudge(model=model)


def _failed_call(
    model: str,
    reason: str,
    *,
    started_at: str,
) -> SemanticJudgeCall:
    return SemanticJudgeCall(
        model=model,
        valid=False,
        verdict=None,
        response={"verdict": False, "reason": reason, "_error": True},
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )


def _validated_semantic_vote(response: dict[str, Any]) -> bool | None:
    """Return a logically consistent rubric vote, or None for a schema violation."""
    boolean_fields = (
        "root_cause_correct",
        "patch_addresses_root_cause",
        "over_broad_suppression",
        "evidence_consistent",
        "regression_risk_acceptable",
        "verdict",
    )
    if any(not isinstance(response.get(field), bool) for field in boolean_fields):
        return None
    if not isinstance(response.get("reason"), str) or not response["reason"].strip():
        return None
    computed = bool(
        response["root_cause_correct"]
        and response["patch_addresses_root_cause"]
        and not response["over_broad_suppression"]
        and response["evidence_consistent"]
        and response["regression_risk_acceptable"]
    )
    return computed if response["verdict"] is computed else None


def _run_judge(
    *,
    model: str,
    prompt: str,
    schema: dict[str, str],
    judge_factory: JudgeFactory,
) -> SemanticJudgeCall:
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        judge = judge_factory(model)
    except Exception as exc:  # noqa: BLE001 -- provider construction failures are invalid votes
        return _failed_call(
            model,
            f"judge_factory_error: {type(exc).__name__}: {exc}",
            started_at=started_at,
        )

    if judge.model != model:
        raise ValueError(f"semantic judge seat requires model {model!r}, got {judge.model!r}")

    built = {"prompt": prompt, "schema": dict(schema), "excerpts": {}}
    try:
        response = judge(built)
    except Exception as exc:  # noqa: BLE001 -- provider call failures are invalid votes
        return _failed_call(
            model,
            f"judge_error: {type(exc).__name__}: {exc}",
            started_at=started_at,
        )
    if not isinstance(response, dict):
        return _failed_call(
            model,
            "judge_error: response is not a JSON object",
            started_at=started_at,
        )

    persisted = dict(response)
    verdict = _validated_semantic_vote(persisted)
    valid = not bool(persisted.get("_error")) and verdict is not None
    return SemanticJudgeCall(
        model=model,
        valid=valid,
        verdict=verdict if valid else None,
        response=persisted,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )


def evaluate_semantic_gate(
    *,
    task_id: str,
    evidence: dict[str, Any],
    judge_factory: JudgeFactory | None = None,
) -> SemanticGateResult:
    """Require all three provider seats, then aggregate their votes 2-of-3."""
    prompt = build_blinded_semantic_prompt(evidence)
    schema = dict(SEMANTIC_RUBRIC)
    factory = judge_factory or _default_judge_factory
    calls = tuple(
        _run_judge(model=model, prompt=prompt, schema=schema, judge_factory=factory)
        for model in SEMANTIC_JUDGE_MODELS
    )
    valid_votes = sum(call.valid for call in calls)
    positive_votes = sum(call.verdict is True for call in calls)
    available = valid_votes == len(SEMANTIC_JUDGE_MODELS)
    return SemanticGateResult(
        task_id=task_id,
        verdict=available and positive_votes >= 2,
        available=available,
        valid_votes=valid_votes,
        positive_votes=positive_votes,
        judge_error=valid_votes != len(SEMANTIC_JUDGE_MODELS),
        prompt=prompt,
        schema=schema,
        models=SEMANTIC_JUDGE_MODELS,
        calls=calls,
    )
