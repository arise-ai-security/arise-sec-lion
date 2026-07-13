"""Authoritative verdict: mechanical AND safety AND regression AND semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from experiments.shared.evaluation.official import (
    CrashSignature,
    MechanicalVerdict,
    ReferenceReplayResult,
    SafetyFloorInput,
    SafetyFloorVerdict,
    evaluate_safety_floor,
)
from experiments.shared.evaluation.regression import (
    RegressionEvidence,
    unavailable_regression,
)
from experiments.shared.evaluation.semantic_gate import (
    JudgeFactory,
    SemanticGateResult,
    evaluate_semantic_gate,
)


if TYPE_CHECKING:
    from pathlib import Path

    from experiments.shared.evaluation.official import CommandEvidence


REPLAY_COUNT = 3


class ReplayRunnerPort(Protocol):
    """Fresh-container replay seam implemented by the external evaluator."""

    def run(
        self,
        *,
        input_dir: Path,
        output_dir: Path,
        evaluation_type: Literal["poc", "patch"],
    ) -> ReferenceReplayResult: ...


@dataclass(frozen=True, slots=True)
class CombinedVerdictInput:
    poc_input_dir: Path
    poc_output_dir: Path
    patch_input_dir: Path
    patch_output_dir: Path
    poc_present: bool
    patch_present: bool
    safety: SafetyFloorInput
    task_id: str
    expected_crash_signature: CrashSignature
    semantic_evidence: dict[str, Any]
    regression: RegressionEvidence | None = None


@dataclass(frozen=True, slots=True)
class MechanicalOutcome:
    """All three independent pre- and post-patch replay outcomes."""

    passed: bool
    poc: MechanicalVerdict
    patch_primary: MechanicalVerdict
    patch_strict: MechanicalVerdict
    patch_generous: MechanicalVerdict
    replay_count: int
    expected_crash_signature: CrashSignature


@dataclass(frozen=True, slots=True)
class CombinedVerdict:
    success: bool
    mechanical: MechanicalOutcome
    safety: SafetyFloorVerdict
    regression: RegressionEvidence
    semantic: SemanticGateResult | None
    semantic_accepted: bool
    poc_replays: tuple[ReferenceReplayResult, ...]
    patch_replays: tuple[ReferenceReplayResult, ...]

    @property
    def poc_evidence(self) -> tuple[CommandEvidence, ...]:
        return tuple(replay.modes["primary"].evidence for replay in self.poc_replays)

    @property
    def patch_evidence(self) -> tuple[CommandEvidence, ...]:
        return tuple(replay.modes["primary"].evidence for replay in self.patch_replays)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "mechanical": {
                "passed": self.mechanical.passed,
                "replay_count": self.mechanical.replay_count,
                "expected_crash_signature": asdict(
                    self.mechanical.expected_crash_signature
                ),
                "poc": asdict(self.mechanical.poc),
                "patch_primary": asdict(self.mechanical.patch_primary),
                "sensitivity": {
                    "strict": asdict(self.mechanical.patch_strict),
                    "generous": asdict(self.mechanical.patch_generous),
                },
            },
            "safety": asdict(self.safety),
            "regression": self.regression.to_dict(),
            "semantic": asdict(self.semantic) if self.semantic is not None else {
                "accepted": False,
                "available": False,
                "skipped": "prior_gate_failed",
            },
            "semantic_accepted": self.semantic_accepted,
            "poc_replays": [asdict(replay) for replay in self.poc_replays],
            "patch_replays": [asdict(replay) for replay in self.patch_replays],
        }


def evaluate_combined_verdict(
    request: CombinedVerdictInput,
    *,
    replay_runner: ReplayRunnerPort,
    judge_factory: JudgeFactory | None = None,
) -> CombinedVerdict:
    """Run six fresh replays, exact-oracle validation, safety, host regression, judges.

    Semantic judges run only after mechanical, safety, and host regression all pass.
    Worker-authored regression prose never substitutes for host regression evidence.
    """
    poc_replays = _run_replays(
        replay_runner,
        input_dir=request.poc_input_dir,
        output_dir=request.poc_output_dir,
        evaluation_type="poc",
    )
    patch_replays = _run_replays(
        replay_runner,
        input_dir=request.patch_input_dir,
        output_dir=request.patch_output_dir,
        evaluation_type="patch",
    )

    poc_modes = tuple(replay.modes["primary"] for replay in poc_replays)
    patch_primary_modes = tuple(replay.modes["primary"] for replay in patch_replays)
    patch_strict_modes = tuple(replay.modes["strict"] for replay in patch_replays)
    patch_generous_modes = tuple(replay.modes["generous"] for replay in patch_replays)

    oracle_matches = (
        request.expected_crash_signature.complete
        and request.expected_crash_signature.access_kind is not None
        and all(
            mode.crash_signature == request.expected_crash_signature for mode in poc_modes
        )
    )
    poc = _aggregate(
        request.poc_present
        and all(mode.verdict.passed for mode in poc_modes)
        and oracle_matches,
        success="all 3 PoC replays matched the exact frozen crash oracle",
        failure=(
            "PoC replay failed or sanitizer class/access/top application frame did not "
            "match the frozen oracle in all 3 fresh containers"
        ),
    )
    patch_primary = _aggregate_modes(
        request.patch_present, patch_primary_modes, "patch primary"
    )
    patch_strict = _aggregate_modes(
        request.patch_present, patch_strict_modes, "patch strict"
    )
    patch_generous = _aggregate_modes(
        request.patch_present, patch_generous_modes, "patch generous"
    )
    mechanical = MechanicalOutcome(
        passed=poc.passed and patch_primary.passed,
        poc=poc,
        patch_primary=patch_primary,
        patch_strict=patch_strict,
        patch_generous=patch_generous,
        replay_count=REPLAY_COUNT,
        expected_crash_signature=request.expected_crash_signature,
    )

    # fresh_base is proven from six distinct container identities, never asserted.
    safety = evaluate_safety_floor(
        replace(
            request.safety,
            fresh_base=_fresh_base_from_replays(poc_replays, patch_replays),
        ),
        tuple(mode.evidence for mode in patch_primary_modes),
    )
    regression = request.regression or unavailable_regression(
        reason="missing frozen host regression plan; evaluation unavailable",
        base_commit="",
    )
    regression_ok = regression.available and regression.passed

    semantic: SemanticGateResult | None = None
    if mechanical.passed and safety.passed and regression_ok:
        semantic = evaluate_semantic_gate(
            task_id=request.task_id,
            evidence={
                **request.semantic_evidence,
                "host_replay": {
                    "expected_crash_signature": asdict(request.expected_crash_signature),
                    "pre_patch": [asdict(replay) for replay in poc_replays],
                    "post_patch": [asdict(replay) for replay in patch_replays],
                },
                "host_regression": regression.to_dict(),
            },
            judge_factory=judge_factory,
        )
    semantic_accepted = semantic is not None and semantic.verdict
    return CombinedVerdict(
        success=mechanical.passed and safety.passed and regression_ok and semantic_accepted,
        mechanical=mechanical,
        safety=safety,
        regression=regression,
        semantic=semantic,
        semantic_accepted=semantic_accepted,
        poc_replays=poc_replays,
        patch_replays=patch_replays,
    )


def _run_replays(
    runner: ReplayRunnerPort,
    *,
    input_dir: Path,
    output_dir: Path,
    evaluation_type: Literal["poc", "patch"],
) -> tuple[ReferenceReplayResult, ...]:
    return tuple(
        runner.run(
            input_dir=input_dir,
            output_dir=output_dir.parent / f"{output_dir.name}-{index}",
            evaluation_type=evaluation_type,
        )
        for index in range(1, REPLAY_COUNT + 1)
    )


def _fresh_base_from_replays(
    poc_replays: tuple[ReferenceReplayResult, ...],
    patch_replays: tuple[ReferenceReplayResult, ...],
) -> bool:
    """True iff six replays each report a distinct non-empty container identity."""
    identities = tuple(
        replay.container_id
        for replay in (*poc_replays, *patch_replays)
        if isinstance(replay.container_id, str) and replay.container_id.strip()
    )
    return len(identities) == REPLAY_COUNT * 2 and len(set(identities)) == REPLAY_COUNT * 2


def _aggregate_modes(
    artifact_present: bool,
    modes: tuple[Any, ...],
    label: str,
) -> MechanicalVerdict:
    passed = artifact_present and len(modes) == REPLAY_COUNT and all(
        mode.verdict.passed for mode in modes
    )
    return _aggregate(
        passed,
        success=f"all {REPLAY_COUNT} {label} replays passed",
        failure=f"{label} artifact absent or at least one of {REPLAY_COUNT} replays failed",
    )


def _aggregate(passed: bool, *, success: str, failure: str) -> MechanicalVerdict:
    return MechanicalVerdict(passed=passed, reason=success if passed else failure)
