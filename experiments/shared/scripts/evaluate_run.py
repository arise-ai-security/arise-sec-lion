"""Compute a run's authoritative verdict and print it as JSON.

The authoritative result is the three-layer combined verdict
(:func:`~experiments.shared.evaluation.combined_verdict.evaluate_combined_verdict`):
``official mechanical AND Arise safety/provenance floor AND independent semantic``.
None of the three layers trusts an agent-authored VERDICT file.

The two live-infra seams are dependency-injected: the fresh-container reference
evaluator adapter and the pinned LLM judge behind the semantic gate. The legacy
contract-only :func:`evaluate_run` criteria verdict is
still computed, but only as a clearly-labelled ``diagnostic_legacy`` field -- it is
NOT authoritative.

``--judge``/``--strict`` now only enrich the legacy diagnostic plane; the
authoritative combined verdict always runs its own independent semantic gate.

Usage::

    python -m experiments.shared.scripts.evaluate_run <run_id> [--no-oracle] [--gold-patch]
    SECBENCH_ROOT=/path/to/SEC-bench python -m experiments.shared.scripts.evaluate_run <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from experiments.shared.evaluation.adapters import (
    SecBenchArtifactAdapter,
    SecBenchEvaluatorAdapter,
)
from experiments.shared.evaluation.combined_verdict import (
    CombinedVerdictInput,
    evaluate_combined_verdict,
)
from experiments.shared.evaluation.regression import (
    ContainerRegressionRunner,
    RegressionEvidence,
    default_secbench_image,
    load_regression_plans_file,
    unavailable_regression,
)
from experiments.shared.evaluation.criteria import declared_path_exists, evaluate_run
from experiments.shared.evaluation.judge import (
    DEFAULT_JUDGE_MODEL,
    DEFAULT_REASONING_EFFORT,
    LLMJudge,
)
from experiments.shared.evaluation.loading import load_run
from experiments.shared.evaluation.official import (
    CrashSignature,
    EvaluationBundleWriter,
    SafetyFloorInput,
    crash_signature,
    sha256_file,
)


if TYPE_CHECKING:
    from experiments.shared.evaluation.combined_verdict import CombinedVerdict, ReplayRunnerPort
    from experiments.shared.evaluation.models import RunData
    from experiments.shared.evaluation.semantic_gate import JudgeFactory


logger = logging.getLogger(__name__)

# experiments/shared/scripts/evaluate_run.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve_secbench_root() -> Path:
    """Locate the SEC-bench checkout that hosts the published evaluator.

    ``SECBENCH_ROOT`` overrides; the default is the sibling checkout next to this
    repository (where ``secb/evaluator/eval_instances.py`` lives).
    """
    env = os.environ.get("SECBENCH_ROOT")
    return Path(env) if env else REPO_ROOT.parent / "SEC-bench"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _patch_target_paths(patch_file: Path, workspace_root: Path) -> tuple[str, ...]:
    """Extract and resolve post-image paths against the sealed source mirror."""
    paths: list[str] = []
    for line in _read_text(patch_file).splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[4:].strip()
        if target in ("", "/dev/null"):
            continue
        if target.startswith(("a/", "b/")):
            target = target[2:]
        paths.append(str((workspace_root / "src" / target).resolve()))
    return tuple(dict.fromkeys(paths))


def _hash_run_artifacts(run_dir: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    """Hash every ``testcase/`` deliverable so the safety floor can bind identity."""
    testcase = run_dir / "testcase"
    paths: list[str] = []
    hashes: dict[str, str] = {}
    if testcase.is_dir():
        for path in sorted(testcase.iterdir()):
            if path.is_file():
                key = str(path)
                paths.append(key)
                hashes[key] = sha256_file(path)
    return tuple(paths), hashes


def _poc_identity(run_dir: Path) -> str:
    """Content identity of the declared PoC artifacts (stable across pre/post fix).

    The safety floor forbids modifying the PoC/repro artifacts, so their content is
    invariant across the exploit and fix evaluations; hashing them here yields the
    same identity on both sides. A stronger in-container binding is validated only
    under a live replay.
    """
    pointer = run_dir / "testcase" / "poc_path.txt"
    digest = hashlib.sha256()
    for path in (pointer, run_dir / "testcase" / "repro.sh"):
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(sha256_file(path).encode("utf-8"))
    for line in _read_text(pointer).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        target = (run_dir / stripped.lstrip("/")).resolve()
        if target.is_file():
            digest.update(stripped.encode("utf-8"))
            digest.update(sha256_file(target).encode("utf-8"))
    return digest.hexdigest()


def _semantic_evidence(run_data: RunData, *, poc_present: bool, patch_present: bool) -> dict:
    """Assemble blinded semantic evidence from host artifacts (no arm identity)."""
    run_dir = run_data.run_dir
    oracle = run_data.cve
    regression_files = sorted((run_dir / "testcase").glob("*regression*"))
    return {
        "poc_present": poc_present,
        "patch_present": patch_present,
        "frozen_oracle": {
            "sanitizer": oracle.sanitizer if oracle is not None else "",
            "sanitizer_report": oracle.sanitizer_report if oracle is not None else "",
            "bug_report": oracle.bug_report if oracle is not None else "",
            "bug_description": oracle.bug_description if oracle is not None else "",
            "base_commit": oracle.base_commit if oracle is not None else "",
        },
        "root_cause_excerpt": _read_text(
            run_dir / "testcase" / "root_cause_analysis.txt"
        )[:16000],
        "patch_plan_excerpt": _read_text(run_dir / "testcase" / "patch_plan.json")[:16000],
        "patch_excerpt": _read_text(run_dir / "testcase" / "model_patch.diff")[:16000],
        "patch_validation_excerpt": _read_text(
            run_dir / "testcase" / "patch_validation_results.txt"
        )[:16000],
        # Worker-authored *regression* files are diagnostic only; host regression
        # evidence is attached separately by the combined verdict after mechanical.
        "worker_regression_files_diagnostic": [
            {"name": path.name, "content": _read_text(path)[:16000]}
            for path in regression_files
            if path.is_file()
        ],
        "report_excerpt": _read_text(run_dir / "testcase" / "security_report.md")[:16000],
    }


def _resolve_regression_evidence(
    run_data: RunData,
    *,
    regression: RegressionEvidence | None = None,
    regression_plans_path: Path | None = None,
) -> RegressionEvidence:
    """Execute the frozen plan inside a fresh patched SEC-bench container.

    Missing plans fail closed. Host-side subprocess execution of plan argv is
    not used — authority requires the suite to run against the patched project
    inside the instance container.
    """
    if regression is not None:
        return regression
    instance_id = (
        run_data.cve.instance_id
        if run_data.cve is not None and run_data.cve.instance_id
        else str(run_data.manifest.get("task") or run_data.run_id)
    )
    base_commit = (
        run_data.cve.base_commit
        if run_data.cve is not None and run_data.cve.base_commit
        else str(run_data.manifest.get("base_commit") or "")
    )
    plans_path = regression_plans_path
    if plans_path is None:
        candidate = run_data.run_dir / "regression_plans.yaml"
        if candidate.is_file():
            plans_path = candidate
    if plans_path is None or not plans_path.is_file():
        return unavailable_regression(
            reason=(
                f"missing frozen host regression plan for "
                f"({instance_id!r}, {base_commit!r}); evaluation unavailable"
            ),
            base_commit=base_commit,
        )
    try:
        plans = load_regression_plans_file(plans_path)
    except (OSError, ValueError) as exc:
        return unavailable_regression(
            reason=f"invalid frozen host regression plan file {plans_path}: {exc}",
            base_commit=base_commit,
        )
    plan = plans.get((instance_id, base_commit))
    if plan is None:
        return unavailable_regression(
            reason=(
                f"no frozen host regression plan for "
                f"({instance_id!r}, {base_commit!r}); evaluation unavailable"
            ),
            base_commit=base_commit,
        )
    patch_file = run_data.run_dir / "testcase" / "model_patch.diff"
    patch_hash = sha256_file(patch_file) if patch_file.is_file() else None
    return ContainerRegressionRunner().run(
        plan,
        patch_file=patch_file if patch_file.is_file() else None,
        patch_hash=patch_hash,
        image=default_secbench_image(instance_id),
    )


def _assemble_combined_input(
    run_data: RunData,
    *,
    regression: RegressionEvidence | None = None,
    regression_plans_path: Path | None = None,
) -> CombinedVerdictInput:
    """Derive the verdict-file-agnostic combined inputs from the loaded run.

    Groundable fields (artifact presence, hashes, patched paths, PoC identity) come
    straight from the run artifacts. ``fresh_base`` is left false here and proven
    later from six distinct replay container identities inside
    :func:`evaluate_combined_verdict`. Host regression is fail-closed without a
    frozen plan and runs inside a fresh patched SEC-bench container.
    """
    run_dir = run_data.run_dir
    testcase = run_dir / "testcase"
    patch_file = testcase / "model_patch.diff"

    patch_present = patch_file.is_file() and patch_file.stat().st_size > 0
    poc_present = declared_path_exists(
        testcase / "poc_path.txt", run_dir, allow_empty=True
    )
    artifact_paths, artifact_hashes = _hash_run_artifacts(run_dir)
    poc_identity = _poc_identity(run_dir)

    safety = SafetyFloorInput(
        workspace_root=str(run_dir),
        artifact_paths=artifact_paths,
        artifact_hashes=artifact_hashes,
        modified_paths=_patch_target_paths(patch_file, run_dir),
        forbidden_paths=(),  # the floor unions the canonical protected set itself
        fresh_base=False,  # proven from six distinct container identities after replay
        pre_patch_exploit_identity=poc_identity,
        post_patch_exploit_identity=poc_identity,
    )
    replay_root = run_dir / "evaluation_replay"
    instance_id = (
        run_data.cve.instance_id
        if run_data.cve is not None and run_data.cve.instance_id
        else str(run_data.manifest.get("task") or run_data.run_id)
    )
    poc_input_dir, patch_input_dir = SecBenchArtifactAdapter().export(
        run_dir=run_dir,
        output_root=replay_root,
        instance_id=instance_id,
    )
    return CombinedVerdictInput(
        poc_input_dir=poc_input_dir,
        poc_output_dir=replay_root / "poc",
        patch_input_dir=patch_input_dir,
        patch_output_dir=replay_root / "patch",
        poc_present=poc_present,
        patch_present=patch_present,
        safety=safety,
        task_id=str(run_data.manifest.get("task") or run_data.run_id),
        expected_crash_signature=(
            crash_signature(run_data.cve.sanitizer_report)
            if run_data.cve is not None
            else CrashSignature(None, None, None)
        ),
        semantic_evidence=_semantic_evidence(
            run_data, poc_present=poc_present, patch_present=patch_present
        ),
        regression=_resolve_regression_evidence(
            run_data,
            regression=regression,
            regression_plans_path=regression_plans_path,
        ),
    )


def build_run_verdict(
    run_data: RunData,
    *,
    replay_runner: ReplayRunnerPort,
    judge_factory: JudgeFactory | None = None,
    legacy_judge: LLMJudge | None = None,
    strict: bool = False,
    persist_bundle: bool = False,
    regression: RegressionEvidence | None = None,
    regression_plans_path: Path | None = None,
) -> dict[str, object]:
    """Compose the authoritative combined verdict plus the legacy diagnostic.

    Args:
        run_data: The loaded run (events + run_dir + manifest + optional cve).
        replay_runner: The injected fresh-container replay seam.
        judge_factory: The injected semantic-judge factory (``None`` -> pinned default).
        legacy_judge: Optional judge for the legacy criteria diagnostic plane only.
        strict: Strict flag for the legacy diagnostic verdict only.
        regression: Optional precomputed host regression evidence (tests/injection).
        regression_plans_path: Optional frozen plan file; missing plan is fail-closed.

    Returns:
        An envelope whose authoritative ``success`` is the combined verdict's; the
        legacy criteria result is preserved under ``diagnostic_legacy``.
    """
    combined = evaluate_combined_verdict(
        _assemble_combined_input(
            run_data,
            regression=regression,
            regression_plans_path=regression_plans_path,
        ),
        replay_runner=replay_runner,
        judge_factory=judge_factory,
    )
    legacy = evaluate_run(run_data, judge=legacy_judge, strict=strict)
    envelope: dict[str, object] = {
        "run_id": str(run_data.run_id),
        "authoritative": "combined_verdict",
        "success": combined.success,
        "combined": combined.to_dict(),
        "diagnostic_legacy": legacy,
    }
    if persist_bundle:
        _persist_evaluation_bundle(run_data, combined, envelope)
    return envelope


def _persist_evaluation_bundle(
    run_data: RunData,
    combined: CombinedVerdict,
    envelope: dict[str, object],
) -> None:
    _, input_hashes = _hash_run_artifacts(run_data.run_dir)
    EvaluationBundleWriter(run_data.run_dir).write(
        provenance={
            "run_id": str(run_data.run_id),
            "task": str(run_data.manifest.get("task") or ""),
            "evaluator": "external_reference_adapter",
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "judge_models": list(combined.semantic.models) if combined.semantic else [],
            "evaluator_hashes": _evaluator_hashes(),
        },
        input_hashes=input_hashes,
        mechanical={
            "poc": combined.mechanical.poc,
            "patch_primary": combined.mechanical.patch_primary,
            "patch_strict": combined.mechanical.patch_strict,
            "patch_generous": combined.mechanical.patch_generous,
        },
        safety=combined.safety,
        command_evidence=combined.poc_evidence + combined.patch_evidence,
        combined_verdict=envelope,
        additional_payloads={
            "reference_replays.json": {
                "poc": [asdict(item) for item in combined.poc_replays],
                "patch": [asdict(item) for item in combined.patch_replays],
            },
            "host_regression.json": combined.regression.to_dict(),
            "semantic_panel.json": (
                asdict(combined.semantic)
                if combined.semantic is not None
                else {"available": False, "skipped": "prior_gate_failed"}
            ),
        },
    )


def _evaluator_hashes() -> dict[str, str]:
    paths = (
        Path(__file__),
        REPO_ROOT / "experiments/shared/evaluation/combined_verdict.py",
        REPO_ROOT / "experiments/shared/evaluation/official.py",
        REPO_ROOT / "experiments/shared/evaluation/semantic_gate.py",
        REPO_ROOT / "experiments/shared/evaluation/judge.py",
        REPO_ROOT / "experiments/shared/evaluation/adapters/secbench.py",
    )
    return {str(path.relative_to(REPO_ROOT)): sha256_file(path) for path in paths}


async def run_verdict(
    run_id: str,
    *,
    with_oracle: bool = True,
    include_gold_patch: bool = False,
    replay_runner: ReplayRunnerPort | None = None,
    judge_factory: JudgeFactory | None = None,
    legacy_judge: LLMJudge | None = None,
    strict: bool = False,
    regression_plans_path: Path | None = None,
) -> dict[str, object]:
    """Load the run and compute its authoritative combined verdict (+ legacy diagnostic)."""
    run_data = await load_run(
        run_id, with_oracle=with_oracle, include_gold_patch=include_gold_patch
    )
    if with_oracle and run_data.cve is None:
        logger.warning(
            "No CVE oracle resolved for run %s (task=%r)",
            run_id,
            run_data.manifest.get("task"),
        )
    base_commit = (
        run_data.cve.base_commit
        if run_data.cve is not None and run_data.cve.base_commit
        else str(run_data.manifest.get("base_commit") or "") or None
    )
    runner = (
        replay_runner
        if replay_runner is not None
        else SecBenchEvaluatorAdapter(
            _resolve_secbench_root(),
            base_commit=base_commit,
        )
    )
    return build_run_verdict(
        run_data,
        replay_runner=runner,
        judge_factory=judge_factory,
        legacy_judge=legacy_judge,
        strict=strict,
        persist_bundle=True,
        regression_plans_path=regression_plans_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the authoritative three-layer combined per-run verdict as JSON."
    )
    parser.add_argument("run_id", help="The run/root (BOSS) aggregate id.")
    parser.add_argument(
        "--no-oracle",
        action="store_true",
        help="Skip CVE-oracle resolution (legacy diagnostic degrades to mechanical-only).",
    )
    parser.add_argument(
        "--gold-patch",
        action="store_true",
        help="Carry the host-side gold patch on the oracle (legacy patch judge only).",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Run the four semantic LLM judges on the LEGACY diagnostic plane.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Strict LEGACY diagnostic verdict (implies --judge); does not affect authority.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_JUDGE_MODEL,
        help=f"Legacy diagnostic judge model (default: {DEFAULT_JUDGE_MODEL}).",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=DEFAULT_REASONING_EFFORT,
        help=f"Legacy diagnostic judge reasoning effort (default: {DEFAULT_REASONING_EFFORT}).",
    )
    args = parser.parse_args(argv)

    legacy_judge = (
        LLMJudge(model=args.model, reasoning_effort=args.reasoning_effort)
        if (args.judge or args.strict)
        else None
    )
    verdict = asyncio.run(
        run_verdict(
            args.run_id,
            with_oracle=not args.no_oracle,
            include_gold_patch=args.gold_patch,
            legacy_judge=legacy_judge,
            strict=args.strict,
        )
    )
    sys.stdout.write(json.dumps(verdict, indent=2, sort_keys=True) + "\n")
    if legacy_judge is not None:
        sys.stderr.write(
            f"legacy judge: model={legacy_judge.model} calls={legacy_judge.calls} "
            f"errors={legacy_judge.errors} usage={legacy_judge.usage} "
            f"cost_usd={round(legacy_judge.cost_usd, 4)}\n"
        )
    return 0 if verdict.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
