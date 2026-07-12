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

import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
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
    """

    cell: str
    task: str
    base_commit: str
    replicate: int
    run_id: str | None
    status: str
    cost: float
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
    """Assembled confirmatory result: paired statistics plus per-arm cost endpoints."""

    pairs: tuple[InstancePair, ...]
    observations: tuple[PairedObservation, ...]
    mcnemar_p: float
    intervals: dict[str, ConfidenceInterval]
    superiority: bool
    n1_cost: CostEndpoints
    b4_cost: CostEndpoints


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
    if replicates < 1:
        raise ValueError("replicates must be >= 1")
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
    """Pair N1 and B4 outcomes by ``(task, base_commit, replicate)``.

    Only instances with exactly one outcome per arm form a pair; an instance
    missing an arm — or carrying duplicates for one arm — is skipped so a
    partial cell can never silently corrupt the paired statistics.
    """
    grouped: dict[tuple[str, str, int], dict[str, list[LaunchOutcome]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for outcome in outcomes:
        key = (outcome.task, outcome.base_commit, outcome.replicate)
        grouped[key][outcome.cell].append(outcome)

    pairs: list[InstancePair] = []
    for (task, base, rep), arms in sorted(grouped.items()):
        n1s = arms.get(n1_cell, [])
        b4s = arms.get(b4_cell, [])
        if len(n1s) == 1 and len(b4s) == 1:
            pairs.append(
                InstancePair(
                    task=task, base_commit=base, replicate=rep, n1=n1s[0], b4=b4s[0]
                )
            )
    return tuple(pairs)


def to_paired_observations(
    pairs: Sequence[InstancePair],
    *,
    success: SuccessKind = "combined",
) -> tuple[PairedObservation, ...]:
    """Project paired instances into confirmatory :class:`PairedObservation` inputs."""

    def ok(outcome: LaunchOutcome) -> bool:
        return outcome.combined_success if success == "combined" else outcome.mechanical_success

    return tuple(
        PairedObservation(
            n1_success=ok(pair.n1),
            b4_success=ok(pair.b4),
            n1_cost=pair.n1.cost,
            b4_cost=pair.b4.cost,
        )
        for pair in pairs
    )


def cost_endpoints_from_outcomes(outcomes: Sequence[LaunchOutcome]) -> CostEndpoints:
    """Per-arm cost/run, cost/official-success, cost/combined-success, completeness.

    Mirrors :func:`experiments.shared.evaluation.cost.cost_endpoints` but sources
    the already-measured per-launch cost/success/completeness, so it stays pure
    over ITT-enrolled outcomes (no event replay, no database). Failures are kept
    in the denominator of cost/run and completeness by construction.
    """
    if not outcomes:
        raise ValueError("at least one outcome is required")
    count = len(outcomes)
    total = sum(o.cost for o in outcomes)
    mechanical = sum(o.mechanical_success for o in outcomes)
    combined = sum(o.combined_success for o in outcomes)
    return CostEndpoints(
        cost_per_run=total / count,
        cost_per_mechanical_success=(total / mechanical if mechanical else None),
        cost_per_combined_success=(total / combined if combined else None),
        completeness_rate=sum(o.completeness_rate for o in outcomes) / count,
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
    return ConfirmatoryReport(
        pairs=pairs,
        observations=observations,
        mcnemar_p=mcnemar_exact(observations),
        intervals=intervals,
        superiority=superiority_supported(difference) if difference is not None else False,
        n1_cost=cost_endpoints_from_outcomes([o for o in outcomes if o.cell == n1_cell]),
        b4_cost=cost_endpoints_from_outcomes([o for o in outcomes if o.cell == b4_cell]),
    )
