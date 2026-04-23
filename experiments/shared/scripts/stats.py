"""Stats primitives for comparative experiments — stdlib-only.

Study-authoring scripts can import these for success rates and bootstrap
confidence intervals without pulling in pandas/numpy. When a study needs
heavier tooling (matplotlib, seaborn, statsmodels), its own `scripts/` can
depend on them locally.
"""

from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence


def success_rate(outcomes: Sequence[bool]) -> float:
    """Return the proportion of True entries, or 0.0 when empty."""
    if not outcomes:
        return 0.0
    return sum(1 for x in outcomes if x) / len(outcomes)


def wilson_score_interval(
    outcomes: Sequence[bool],
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    More accurate than the normal approximation for small n or extreme
    rates. Returns ``(lower, upper)`` in [0.0, 1.0]; both bounds equal the
    observed rate when ``outcomes`` has no elements.
    """
    if not outcomes:
        return 0.0, 0.0
    n = len(outcomes)
    p_hat = success_rate(outcomes)
    z = _normal_quantile(1 - (1 - confidence) / 2)

    denom = 1 + (z * z) / n
    center = (p_hat + (z * z) / (2 * n)) / denom
    half = (z / denom) * math.sqrt((p_hat * (1 - p_hat) / n) + (z * z) / (4 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def bootstrap_ci(
    outcomes: Sequence[bool],
    *,
    confidence: float = 0.95,
    samples: int = 2000,
    seed: int | None = 0,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the success rate.

    Returns ``(lower, upper)``. Seeded by default so studies get
    deterministic numbers — override ``seed=None`` for fresh randomness.
    """
    if not outcomes:
        return 0.0, 0.0
    rng = random.Random(seed)  # noqa: S311 - deterministic stats, not crypto
    values = [1 if x else 0 for x in outcomes]
    n = len(values)
    rates: list[float] = []
    for _ in range(samples):
        draw = [values[rng.randrange(n)] for _ in range(n)]
        rates.append(sum(draw) / n)
    rates.sort()
    lower_idx = int((1 - confidence) / 2 * samples)
    upper_idx = int((1 + confidence) / 2 * samples) - 1
    return rates[max(0, lower_idx)], rates[min(samples - 1, upper_idx)]


def _normal_quantile(p: float) -> float:
    """Beasley-Springer-Moro inverse CDF for the standard normal.

    Good enough (six-digit accurate) for CI construction without scipy.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"probability must be in (0, 1), got {p}")

    a = (
        -3.969683028665376e1,
        2.209460984245205e2,
        -2.759285104469687e2,
        1.383577518672690e2,
        -3.066479806614716e1,
        2.506628277459239,
    )
    b = (
        -5.447609879822406e1,
        1.615858368580409e2,
        -1.556989798598866e2,
        6.680131188771972e1,
        -1.328068155288572e1,
    )
    c = (
        -7.784894002430293e-3,
        -3.223964580411365e-1,
        -2.400758277161838,
        -2.549732539343734,
        4.374664141464968,
        2.938163982698783,
    )
    d = (
        7.784695709041462e-3,
        3.224671290700398e-1,
        2.445134137142996,
        3.754408661907416,
    )

    p_low = 0.02425
    p_high = 1 - p_low

    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(
        (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
        / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    )
