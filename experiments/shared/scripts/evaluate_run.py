"""Compute a run's authoritative verdict and print it as JSON.

The authoritative result is the three-layer combined verdict
(:func:`~experiments.shared.evaluation.combined_verdict.evaluate_combined_verdict`):
``official mechanical AND Arise safety/provenance floor AND independent semantic``.
None of the three layers trusts an agent-authored VERDICT file.

The two live-infra seams are dependency-injected: the fresh-container
:class:`~experiments.shared.evaluation.official.SecBenchReplayRunner` (which shells
out to the published SEC-bench evaluator) and the pinned LLM judge behind the
semantic gate. The legacy contract-only :func:`evaluate_run` criteria verdict is
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
from pathlib import Path
from typing import TYPE_CHECKING

from experiments.shared.evaluation.combined_verdict import (
    CombinedVerdictInput,
    evaluate_combined_verdict,
)
from experiments.shared.evaluation.criteria import declared_path_exists, evaluate_run
from experiments.shared.evaluation.judge import (
    DEFAULT_JUDGE_MODEL,
    DEFAULT_REASONING_EFFORT,
    LLMJudge,
)
from experiments.shared.evaluation.loading import load_run
from experiments.shared.evaluation.official import (
    SafetyFloorInput,
    SecBenchReplayRunner,
    sha256_file,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from experiments.shared.evaluation.combined_verdict import ReplayRunnerPort
    from experiments.shared.evaluation.models import RunData
    from experiments.shared.evaluation.semantic_gate import SemanticJudge


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


def _patch_target_paths(patch_file: Path) -> tuple[str, ...]:
    """Extract the post-image target paths a unified diff modifies."""
    paths: list[str] = []
    for line in _read_text(patch_file).splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[4:].strip()
        if target in ("", "/dev/null"):
            continue
        if target.startswith(("a/", "b/")):
            target = target[2:]
        paths.append(target)
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
    for line in _read_text(pointer).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        target = (run_dir / stripped.lstrip("/")).resolve()
        if target.is_file():
            digest.update(sha256_file(target).encode("utf-8"))
    return digest.hexdigest()


def _semantic_evidence(run_data: RunData, *, poc_present: bool, patch_present: bool) -> dict:
    """Assemble blinded semantic evidence from host artifacts (no arm identity)."""
    run_dir = run_data.run_dir
    return {
        "task_id": str(run_data.manifest.get("task") or run_data.run_id),
        "poc_present": poc_present,
        "patch_present": patch_present,
        "patch_excerpt": _read_text(run_dir / "testcase" / "model_patch.diff")[:4000],
        "report_excerpt": _read_text(run_dir / "testcase" / "security_report.md")[:4000],
    }


def _expected_exit_code(run_data: RunData) -> int | None:
    """The dataset's expected patched exit code (medium-mode oracle), if declared."""
    value = run_data.manifest.get("expected_exit_code")
    return value if isinstance(value, int) else None


def _assemble_combined_input(run_data: RunData) -> CombinedVerdictInput:
    """Derive the verdict-file-agnostic combined inputs from the loaded run.

    Groundable fields (artifact presence, hashes, patched paths, PoC identity) come
    straight from the run artifacts. ``fresh_base`` is asserted from the injected
    replay runner's fresh-container contract; the export dir layout and the dataset
    exit-code oracle are only fully exercised under a live SEC-bench replay.
    """
    run_dir = run_data.run_dir
    testcase = run_dir / "testcase"
    patch_file = testcase / "model_patch.diff"

    patch_present = patch_file.is_file() and patch_file.stat().st_size > 0
    poc_present = declared_path_exists(testcase / "poc_path.txt", run_dir)
    artifact_paths, artifact_hashes = _hash_run_artifacts(run_dir)
    poc_identity = _poc_identity(run_dir)

    safety = SafetyFloorInput(
        workspace_root=str(run_dir),
        artifact_paths=artifact_paths,
        artifact_hashes=artifact_hashes,
        modified_paths=_patch_target_paths(patch_file),
        forbidden_paths=(),  # the floor unions the canonical protected set itself
        fresh_base=True,  # SecBenchReplayRunner creates fresh eval containers
        pre_patch_exploit_identity=poc_identity,
        post_patch_exploit_identity=poc_identity,
    )
    replay_root = run_dir / "evaluation_replay"
    return CombinedVerdictInput(
        poc_input_dir=testcase,
        poc_output_dir=replay_root / "poc",
        patch_input_dir=testcase,
        patch_output_dir=replay_root / "patch",
        poc_present=poc_present,
        patch_present=patch_present,
        expected_exit_code=_expected_exit_code(run_data),
        safety=safety,
        task_id=str(run_data.manifest.get("task") or run_data.run_id),
        semantic_evidence=_semantic_evidence(
            run_data, poc_present=poc_present, patch_present=patch_present
        ),
    )


def build_run_verdict(
    run_data: RunData,
    *,
    replay_runner: ReplayRunnerPort,
    judge_factory: Callable[[], SemanticJudge] | None = None,
    legacy_judge: LLMJudge | None = None,
    strict: bool = False,
) -> dict[str, object]:
    """Compose the authoritative combined verdict plus the legacy diagnostic.

    Args:
        run_data: The loaded run (events + run_dir + manifest + optional cve).
        replay_runner: The injected fresh-container replay seam.
        judge_factory: The injected semantic-judge factory (``None`` -> pinned default).
        legacy_judge: Optional judge for the legacy criteria diagnostic plane only.
        strict: Strict flag for the legacy diagnostic verdict only.

    Returns:
        An envelope whose authoritative ``success`` is the combined verdict's; the
        legacy criteria result is preserved under ``diagnostic_legacy``.
    """
    combined = evaluate_combined_verdict(
        _assemble_combined_input(run_data),
        replay_runner=replay_runner,
        judge_factory=judge_factory,
    )
    legacy = evaluate_run(run_data, judge=legacy_judge, strict=strict)
    return {
        "run_id": str(run_data.run_id),
        "authoritative": "combined_verdict",
        "success": combined.success,
        "combined": combined.to_dict(),
        "diagnostic_legacy": legacy,
    }


async def run_verdict(
    run_id: str,
    *,
    with_oracle: bool = True,
    include_gold_patch: bool = False,
    replay_runner: ReplayRunnerPort | None = None,
    judge_factory: Callable[[], SemanticJudge] | None = None,
    legacy_judge: LLMJudge | None = None,
    strict: bool = False,
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
    runner = (
        replay_runner
        if replay_runner is not None
        else SecBenchReplayRunner(_resolve_secbench_root())
    )
    return build_run_verdict(
        run_data,
        replay_runner=runner,
        judge_factory=judge_factory,
        legacy_judge=legacy_judge,
        strict=strict,
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
