"""Operational confirmatory runner: pair, interleave, enroll (ITT), report.

Pure orchestration around the predeclared statistics in
:mod:`experiments.shared.evaluation.confirmatory`. Run LAUNCH is injected as a
callable so the same logic drives a fake launcher under test and the real
harness in production; nothing in this module spawns a run itself.

The runner closes the four gaps called out in ``N1_VS_B4_STRICT_FINDINGS.md``:
frozen N1/B4 arms paired by ``(task, base_commit)``, a deterministic randomized-
interleaved launch order (so temporal/host-contention drift cannot align with
one arm), intention-to-treat enrollment that RETAINS failures and timeouts
instead of curating to the latest success, and report integration over the
predeclared cost/success endpoints.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from experiments.shared.evaluation.confirmatory import (
    PairedObservation,
    mcnemar_exact,
    paired_bootstrap,
    superiority_supported,
)
from experiments.shared.evaluation.cost import CostEndpoints


if TYPE_CHECKING:
    from collections.abc import Sequence

    from experiments.shared.evaluation.confirmatory import ConfidenceInterval


SuccessKind = Literal["combined", "mechanical"]


@dataclass(frozen=True, slots=True)
class ConfirmatoryCandidate:
    """One frozen arm assignment to launch, keyed to a (task, base_commit) instance."""

    cell: str
    task: str
    base_commit: str
    replicate: int = 0


@dataclass(frozen=True, slots=True)
class LaunchOutcome:
    """A single completed launch, retained under intention-to-treat.

    ``status`` records the real terminal disposition (``completed`` /
    ``failed`` / ``timeout`` / ...). Failures and timeouts are retained with
    their real (usually False) success flags — never curated away.
    ``cost=None`` represents unavailable usage and is rejected by cost analysis;
    it must never be replaced with a numeric zero.
    """

    cell: str
    task: str
    base_commit: str
    replicate: int
    run_id: str | None
    status: str
    cost: float | None
    mechanical_success: bool
    combined_success: bool
    completeness_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class InstancePair:
    """The N1 and B4 outcomes for one ``(task, base_commit, replicate)`` instance."""

    task: str
    base_commit: str
    replicate: int
    n1: LaunchOutcome
    b4: LaunchOutcome


@dataclass(frozen=True, slots=True)
class ConfirmatoryReport:
    """Assembled confirmatory result: paired statistics plus per-arm cost endpoints.

    Primary ITT success analysis remains available when secondary cost is
    incomplete. Cost endpoints report ``cost_available=false`` and the exact
    missing assignment keys instead of imputing zero or dropping pairs.
    """

    pairs: tuple[InstancePair, ...]
    observations: tuple[PairedObservation, ...]
    mcnemar_p: float
    intervals: dict[str, ConfidenceInterval]
    superiority: bool
    n1_cost: CostEndpoints
    b4_cost: CostEndpoints
    cost_available: bool
    missing_cost_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Render the report as JSON-compatible primitives."""
        return asdict(self)


LaunchFn = Callable[[ConfirmatoryCandidate], LaunchOutcome]


def interleaved_launch_order(
    instances: Sequence[tuple[str, str]],
    *,
    n1_cell: str,
    b4_cell: str,
    seed: int,
    replicates: int = 1,
) -> tuple[ConfirmatoryCandidate, ...]:
    """Deterministic randomized-interleaved launch order for both arms.

    A single explicit ``seed`` fixes the instance sequence, the replicate order,
    and which arm leads each pair. The two arms are emitted adjacently per
    ``(instance, replicate)`` so neither arm ever runs more than two launches in
    a row — bounding the temporal/host-contention drift that a block-by-arm
    schedule would confound with the treatment.

    Args:
        instances: ``(task, base_commit)`` pairs to compare.
        n1_cell: Naive-baseline arm cell name.
        b4_cell: Adaptive arm cell name.
        seed: Explicit seed making the whole order reproducible.
        replicates: Samples per instance per arm.

    Returns:
        The flattened, interleaved candidate launch order.
    """
    if not instances:
        raise ValueError("at least one confirmatory instance is required")
    if n1_cell == b4_cell:
        raise ValueError("N1 and B4 cells must be distinct")
    if replicates < 1:
        raise ValueError("replicates must be >= 1")
    if any(not task.strip() or not base.strip() for task, base in instances):
        raise ValueError("every confirmatory instance requires a task and base_commit")
    if len(set(instances)) != len(instances):
        raise ValueError("confirmatory instances must be unique by (task, base_commit)")
    rng = random.Random(seed)
    units = [(task, base, rep) for (task, base) in instances for rep in range(replicates)]
    rng.shuffle(units)
    order: list[ConfirmatoryCandidate] = []
    for task, base, rep in units:
        arms = (n1_cell, b4_cell) if rng.random() < 0.5 else (b4_cell, n1_cell)
        order.extend(
            ConfirmatoryCandidate(cell=cell, task=task, base_commit=base, replicate=rep)
            for cell in arms
        )
    return tuple(order)


def enroll_intention_to_treat(outcomes: Sequence[LaunchOutcome]) -> tuple[LaunchOutcome, ...]:
    """Enroll EVERY launch, failures and timeouts included (intention-to-treat).

    Contrast :func:`curate_latest_success`, the rejected snapshot policy that
    drops non-success launches and keeps only the newest success per instance —
    the selection bias called out in ``N1_VS_B4_STRICT_FINDINGS.md``. ITT keeps
    the launched set as-is, ordered deterministically for reproducible pairing.
    """
    return tuple(sorted(outcomes, key=lambda o: (o.task, o.base_commit, o.replicate, o.cell)))


def curate_latest_success(outcomes: Sequence[LaunchOutcome]) -> tuple[LaunchOutcome, ...]:
    """The REJECTED latest-success curation, kept only as an intention-to-treat contrast.

    Drops every non-success launch and keeps one success per
    ``(cell, task, base_commit)`` — reproducing the biased snapshot that ITT
    avoids. Present so the difference is explicit and testable, not so it is used.
    """
    latest: dict[tuple[str, str, str], LaunchOutcome] = {}
    for outcome in outcomes:
        if not outcome.combined_success:
            continue
        key = (outcome.cell, outcome.task, outcome.base_commit)
        current = latest.get(key)
        if current is None or outcome.replicate >= current.replicate:
            latest[key] = outcome
    return tuple(sorted(latest.values(), key=lambda o: (o.task, o.base_commit, o.cell)))


def pair_by_instance(
    outcomes: Sequence[LaunchOutcome],
    *,
    n1_cell: str,
    b4_cell: str,
) -> tuple[InstancePair, ...]:
    """Pair exact, nonempty N1/B4 assignments or fail the confirmatory study.

    Both arms must contain the same ``(task, base_commit, replicate)`` keys and
    exactly one outcome for every key. Missing arms, divergent base commits,
    duplicate launches, and undeclared cells are integrity failures rather than
    observations that may be skipped.
    """
    if not outcomes:
        raise ValueError("at least one outcome per arm is required")
    if n1_cell == b4_cell:
        raise ValueError("N1 and B4 cells must be distinct")
    expected_cells = {n1_cell, b4_cell}
    unexpected_cells = sorted({outcome.cell for outcome in outcomes} - expected_cells)
    if unexpected_cells:
        raise ValueError(f"outcomes contain undeclared cells: {unexpected_cells}")

    grouped: dict[tuple[str, str, int], dict[str, list[LaunchOutcome]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for outcome in outcomes:
        key = (outcome.task, outcome.base_commit, outcome.replicate)
        grouped[key][outcome.cell].append(outcome)

    n1_keys = {key for key, arms in grouped.items() if arms.get(n1_cell)}
    b4_keys = {key for key, arms in grouped.items() if arms.get(b4_cell)}
    if not n1_keys or not b4_keys:
        raise ValueError("both N1 and B4 must have at least one outcome")
    if n1_keys != b4_keys:
        missing_n1 = sorted(b4_keys - n1_keys)
        missing_b4 = sorted(n1_keys - b4_keys)
        raise ValueError(
            "N1/B4 assignment sets differ; "
            f"missing from {n1_cell}: {missing_n1}; missing from {b4_cell}: {missing_b4}"
        )

    pairs: list[InstancePair] = []
    for task, base, rep in sorted(n1_keys):
        arms = grouped[(task, base, rep)]
        n1s = arms.get(n1_cell, [])
        b4s = arms.get(b4_cell, [])
        if len(n1s) != 1 or len(b4s) != 1:
            raise ValueError(
                f"assignment {(task, base, rep)!r} requires exactly one outcome per arm; "
                f"found {n1_cell}={len(n1s)}, {b4_cell}={len(b4s)}"
            )
        pairs.append(
            InstancePair(
                task=task, base_commit=base, replicate=rep, n1=n1s[0], b4=b4s[0]
            )
        )
    return tuple(pairs)


def _is_missing_cost(outcome: LaunchOutcome) -> bool:
    """True when usage is unknown, including the legacy ``0.0``+zero-completeness sentinel."""
    return outcome.cost is None or (
        outcome.cost == 0.0 and outcome.completeness_rate == 0.0
    )


def _optional_cost(outcome: LaunchOutcome) -> float | None:
    """Return measured cost, or None when usage is missing (never coerce to 0.0)."""
    if _is_missing_cost(outcome):
        return None
    if outcome.cost is None or not math.isfinite(outcome.cost) or outcome.cost < 0:
        raise ValueError(
            f"launch cost must be finite and non-negative for {outcome.cell}/{outcome.task}"
        )
    return outcome.cost


def _assignment_key(outcome: LaunchOutcome) -> str:
    return f"{outcome.cell}/{outcome.task}/{outcome.base_commit}/r{outcome.replicate}"


def to_paired_observations(
    pairs: Sequence[InstancePair],
    *,
    success: SuccessKind = "combined",
) -> tuple[PairedObservation, ...]:
    """Project paired instances into confirmatory :class:`PairedObservation` inputs.

    Success flags always project. Missing costs become ``None`` so primary ITT
    success statistics remain computable while secondary cost endpoints report
    unavailability separately.
    """

    def ok(outcome: LaunchOutcome) -> bool:
        return outcome.combined_success if success == "combined" else outcome.mechanical_success

    return tuple(
        PairedObservation(
            n1_success=ok(pair.n1),
            b4_success=ok(pair.b4),
            n1_cost=_optional_cost(pair.n1),
            b4_cost=_optional_cost(pair.b4),
        )
        for pair in pairs
    )


def cost_endpoints_from_outcomes(outcomes: Sequence[LaunchOutcome]) -> CostEndpoints:
    """Per-arm cost/run, cost/official-success, cost/combined-success, completeness.

    Mirrors :func:`experiments.shared.evaluation.cost.cost_endpoints` but sources
    the already-measured per-launch cost/success/completeness, so it stays pure
    over ITT-enrolled outcomes (no event replay, no database). Failures are kept
    in the denominator of cost/run and completeness by construction. Missing
    usage never becomes zero: the endpoint is marked unavailable instead.
    """
    if not outcomes:
        raise ValueError("at least one outcome is required")
    missing = tuple(
        _assignment_key(outcome) for outcome in outcomes if _is_missing_cost(outcome)
    )
    completeness = sum(o.completeness_rate for o in outcomes) / len(outcomes)
    if missing:
        return CostEndpoints(
            cost_per_run=None,
            cost_per_mechanical_success=None,
            cost_per_combined_success=None,
            completeness_rate=completeness,
            cost_available=False,
            missing_assignment_keys=missing,
        )
    count = len(outcomes)
    total = sum(float(_optional_cost(outcome) or 0.0) for outcome in outcomes)
    mechanical = sum(o.mechanical_success for o in outcomes)
    combined = sum(o.combined_success for o in outcomes)
    return CostEndpoints(
        cost_per_run=total / count,
        cost_per_mechanical_success=(total / mechanical if mechanical else None),
        cost_per_combined_success=(total / combined if combined else None),
        completeness_rate=completeness,
        cost_available=True,
        missing_assignment_keys=(),
    )


def run_confirmatory_study(
    instances: Sequence[tuple[str, str]],
    *,
    n1_cell: str,
    b4_cell: str,
    seed: int,
    launch: LaunchFn,
    replicates: int = 1,
    success: SuccessKind = "combined",
    bootstrap_samples: int = 10_000,
) -> ConfirmatoryReport:
    """Drive one confirmatory study end-to-end with an INJECTED launcher.

    The order is deterministic given ``seed``; every launch is enrolled under
    intention-to-treat (failures/timeouts retained); pairs feed the predeclared
    McNemar + paired-bootstrap statistics and both arms' cost endpoints.
    ``launch`` is the only side-effecting seam — a fake in tests, the harness in
    production — so this function is fully unit-testable and never spawns a run.
    """
    order = interleaved_launch_order(
        instances, n1_cell=n1_cell, b4_cell=b4_cell, seed=seed, replicates=replicates
    )
    outcomes = enroll_intention_to_treat([launch(candidate) for candidate in order])
    pairs = pair_by_instance(outcomes, n1_cell=n1_cell, b4_cell=b4_cell)
    observations = to_paired_observations(pairs, success=success)
    intervals = (
        paired_bootstrap(observations, samples=bootstrap_samples, seed=seed)
        if observations
        else {}
    )
    difference = intervals.get("success_difference")
    n1_cost = cost_endpoints_from_outcomes([o for o in outcomes if o.cell == n1_cell])
    b4_cost = cost_endpoints_from_outcomes([o for o in outcomes if o.cell == b4_cell])
    missing_cost_keys = tuple(
        sorted({*n1_cost.missing_assignment_keys, *b4_cost.missing_assignment_keys})
    )
    return ConfirmatoryReport(
        pairs=pairs,
        observations=observations,
        mcnemar_p=mcnemar_exact(observations),
        intervals=intervals,
        superiority=superiority_supported(difference) if difference is not None else False,
        n1_cost=n1_cost,
        b4_cost=b4_cost,
        cost_available=n1_cost.cost_available and b4_cost.cost_available,
        missing_cost_keys=missing_cost_keys,
    )


def main(argv: list[str] | None = None) -> int:
    """Run a declared paired confirmatory study through the production adapter."""
    parser = argparse.ArgumentParser(description="Run paired N1/B4 confirmatory experiments.")
    parser.add_argument("--study", required=True)
    parser.add_argument("--n1-cell", default="N1")
    parser.add_argument("--b4-cell", default="B4")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--replicates", type=int)
    parser.add_argument("--bootstrap-samples", type=int)
    args = parser.parse_args(argv)

    from experiments.shared.evaluation.adapters.arise_confirmatory import (
        AriseConfirmatoryAdapter,
    )

    adapter = AriseConfirmatoryAdapter(
        study_id=args.study,
        n1_cell=args.n1_cell,
        b4_cell=args.b4_cell,
    )
    frozen_seed, frozen_replicates, frozen_bootstrap_samples = adapter.frozen_parameters()
    supplied = (args.seed, args.replicates, args.bootstrap_samples)
    frozen = (frozen_seed, frozen_replicates, frozen_bootstrap_samples)
    labels = ("seed", "replicates", "bootstrap_samples")
    drift = [
        f"{label}: supplied={value} frozen={expected}"
        for label, value, expected in zip(labels, supplied, frozen, strict=True)
        if value is not None and value != expected
    ]
    if drift:
        raise ValueError(
            "confirmatory CLI parameters drift from preregistration: " + "; ".join(drift)
        )
    report = run_confirmatory_study(
        adapter.instances(),
        n1_cell=args.n1_cell,
        b4_cell=args.b4_cell,
        seed=frozen_seed,
        launch=adapter,
        replicates=frozen_replicates,
        bootstrap_samples=frozen_bootstrap_samples,
    )
    sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
