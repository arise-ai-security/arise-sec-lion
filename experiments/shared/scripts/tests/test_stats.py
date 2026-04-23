"""Tests for the stdlib-only stats primitives."""

from __future__ import annotations

from experiments.shared.scripts.stats import (
    bootstrap_ci,
    success_rate,
    wilson_score_interval,
)


def test_success_rate_empty_returns_zero() -> None:
    assert success_rate([]) == 0.0


def test_success_rate_counts_trues() -> None:
    assert success_rate([True, False, True, True]) == 0.75


def test_wilson_interval_contains_point_estimate() -> None:
    outcomes = [True] * 7 + [False] * 3
    lower, upper = wilson_score_interval(outcomes, confidence=0.95)
    assert lower <= success_rate(outcomes) <= upper


def test_wilson_interval_edge_cases() -> None:
    # All successes → upper bound is 1.0; lower bound is < 1.
    lower, upper = wilson_score_interval([True, True, True], confidence=0.95)
    assert upper == 1.0 or 0.9 < upper <= 1.0
    assert 0.0 <= lower < 1.0


def test_bootstrap_ci_is_deterministic_with_seed() -> None:
    outcomes = [True, False, True, True, False, True]
    a = bootstrap_ci(outcomes, seed=42, samples=500)
    b = bootstrap_ci(outcomes, seed=42, samples=500)
    assert a == b


def test_bootstrap_ci_bounds_sensible() -> None:
    outcomes = [True] * 8 + [False] * 2
    lower, upper = bootstrap_ci(outcomes, seed=42, samples=2000)
    assert 0.0 <= lower <= 0.8 <= upper <= 1.0
