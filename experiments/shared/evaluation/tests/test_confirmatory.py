"""Tests for predeclared paired confirmatory statistics."""

import pytest

from experiments.shared.evaluation.confirmatory import (
    ConfidenceInterval,
    PairedObservation,
    mcnemar_exact,
    paired_bootstrap,
    required_pairs_from_discordance,
    superiority_supported,
)


def test_mcnemar_uses_only_discordant_pairs() -> None:
    # Given: One B4-only pass, three N1-only passes, and concordant pairs
    observations = (
        PairedObservation(False, True, 1, 2),
        PairedObservation(True, False, 1, 2),
        PairedObservation(True, False, 1, 2),
        PairedObservation(True, False, 1, 2),
        PairedObservation(True, True, 1, 2),
        PairedObservation(False, False, 1, 2),
    )

    # When/Then: The exact two-sided p-value matches 1-vs-3 discordance
    assert mcnemar_exact(observations) == pytest.approx(0.625)


def test_paired_bootstrap_preserves_pairing_and_reports_requested_metrics() -> None:
    # Given: Four paired outcomes and costs
    observations = (
        PairedObservation(True, True, 1, 2),
        PairedObservation(False, True, 1, 2),
        PairedObservation(False, False, 1, 2),
        PairedObservation(True, True, 1, 2),
    )

    # When: Paired bootstrap intervals are computed
    intervals = paired_bootstrap(observations, samples=200, seed=7)

    # Then: All predeclared paired endpoints are present
    assert intervals["success_difference"].estimate == pytest.approx(0.25)
    assert intervals["cost_ratio"].estimate == pytest.approx(2.0)
    assert "cost_per_valid_fix_ratio" in intervals


def test_superiority_requires_positive_lower_bound_and_five_point_estimate() -> None:
    # Given/When/Then: Both predeclared conditions are necessary
    assert superiority_supported(ConfidenceInterval(0.05, 0.001, 0.10))
    assert not superiority_supported(ConfidenceInterval(0.04, 0.001, 0.08))
    assert not superiority_supported(ConfidenceInterval(0.08, 0.0, 0.16))


def test_sample_size_uses_development_discordance() -> None:
    # Given/When: Clean development discordance and a five-point target
    pairs = required_pairs_from_discordance(b4_only_rate=0.15, n1_only_rate=0.10)

    # Then: A finite positive paired sample size is produced
    assert pairs > 0
