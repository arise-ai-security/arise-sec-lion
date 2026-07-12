"""Predeclared paired statistics for confirmatory N1-versus-B4 analysis."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from statistics import NormalDist


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    estimate: float
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class PairedObservation:
    n1_success: bool
    b4_success: bool
    n1_cost: float
    b4_cost: float


def mcnemar_exact(observations: tuple[PairedObservation, ...]) -> float:
    """Two-sided exact McNemar p-value from paired binary outcomes."""
    b4_only = sum(item.b4_success and not item.n1_success for item in observations)
    n1_only = sum(item.n1_success and not item.b4_success for item in observations)
    discordant = b4_only + n1_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(b4_only, n1_only) + 1))
    return min(1.0, 2 * tail / (2**discordant))


def paired_bootstrap(
    observations: tuple[PairedObservation, ...],
    *,
    samples: int = 10_000,
    seed: int = 0,
) -> dict[str, ConfidenceInterval]:
    """Paired percentile CIs for success difference, cost ratio, and cost/valid-fix."""
    if not observations:
        raise ValueError("at least one paired observation is required")
    if samples < 1:
        raise ValueError("samples must be positive")
    rng = random.Random(seed)
    metrics = {"success_difference": [], "cost_ratio": [], "cost_per_valid_fix_ratio": []}
    count = len(observations)
    for _ in range(samples):
        sample = [observations[rng.randrange(count)] for _ in range(count)]
        values = _metrics(sample)
        for key, value in values.items():
            if math.isfinite(value):
                metrics[key].append(value)
    point = _metrics(list(observations))
    return {
        key: ConfidenceInterval(
            estimate=point[key],
            lower=_percentile(values, 0.025),
            upper=_percentile(values, 0.975),
        )
        for key, values in metrics.items()
        if values
    }


def superiority_supported(success_difference: ConfidenceInterval) -> bool:
    """Apply the predeclared B4 superiority rule."""
    return success_difference.lower > 0 and success_difference.estimate >= 0.05


def required_pairs_from_discordance(
    *,
    b4_only_rate: float,
    n1_only_rate: float,
    minimum_difference: float = 0.05,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Approximate paired sample size using clean development discordance."""
    discordance = b4_only_rate + n1_only_rate
    if discordance <= 0 or minimum_difference <= 0:
        raise ValueError("positive discordance and minimum_difference are required")
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    variance_alt = discordance - minimum_difference**2
    numerator = (
        z_alpha * math.sqrt(discordance)
        + z_power * math.sqrt(max(variance_alt, 0.0))
    ) ** 2
    return math.ceil(numerator / minimum_difference**2)


def _metrics(observations: list[PairedObservation]) -> dict[str, float]:
    count = len(observations)
    n1_successes = sum(item.n1_success for item in observations)
    b4_successes = sum(item.b4_success for item in observations)
    n1_cost = sum(item.n1_cost for item in observations)
    b4_cost = sum(item.b4_cost for item in observations)
    n1_cost_per_fix = n1_cost / n1_successes if n1_successes else math.inf
    b4_cost_per_fix = b4_cost / b4_successes if b4_successes else math.inf
    return {
        "success_difference": (b4_successes - n1_successes) / count,
        "cost_ratio": b4_cost / n1_cost if n1_cost else math.inf,
        "cost_per_valid_fix_ratio": (
            b4_cost_per_fix / n1_cost_per_fix if math.isfinite(n1_cost_per_fix) else math.inf
        ),
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]
