"""Tests for cache-hit-rate metrics."""

import pytest

from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.tests.builders import RunBuilder


def test_cache_rate_by_node(tmp_path) -> None:
    """Per-role rate = read/prompt; overall is pooled across events."""
    # Given: boss LLM (900/1000) + worker (1000/2000)
    builder = RunBuilder()
    boss = builder.boss()
    worker = builder.agent("worker", boss, "[Builder] x")
    builder.tokens(boss, cost=0.1, prompt=1000, cache_read=900)
    builder.worker_cost(worker, cost=0.8, prompt=2000, cache_read=1000)
    # When
    result = BefRunEvaluator(builder.run_data(tmp_path)).cache_rate_by_node()
    # Then
    assert result.by["BOSS"] == pytest.approx(0.9)
    assert result.by["WORKER"] == pytest.approx(0.5)
    assert result.overall == pytest.approx(1900 / 3000)


def test_cache_rate_none_when_no_prompt_tokens(tmp_path) -> None:
    """A group with zero prompt tokens has an undefined (None) rate."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    builder.tokens(boss, cost=0.0, prompt=0, cache_read=0)
    # When
    result = BefRunEvaluator(builder.run_data(tmp_path)).cache_rate_by_node()
    # Then
    assert result.by["BOSS"] is None
    assert result.overall is None


def test_cache_rate_by_bef(tmp_path) -> None:
    """Worker cache tokens are attributed to the worker's BEF subtree."""
    # Given: boss → [Fixer] manager → [Patch-Applier] worker (750/1000)
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Fixer] fix")
    worker = builder.agent("worker", manager, "[Patch-Applier] patch")
    builder.worker_cost(worker, cost=0.5, prompt=1000, cache_read=750)
    # When
    result = BefRunEvaluator(builder.run_data(tmp_path)).cache_rate_by_bef()
    # Then
    assert result.by["Fixer"] == pytest.approx(0.75)
