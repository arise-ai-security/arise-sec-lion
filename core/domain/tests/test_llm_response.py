"""Tests for LLM response value objects, focused on cache-token accounting."""

from core.domain.values.llm_response import LLMUsage


def test_llm_usage_cache_tokens_default_to_zero() -> None:
    # Given / When: a usage built without cache fields (existing call sites)
    usage = LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    # Then: cache token fields default to zero, preserving existing behavior
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0


def test_llm_usage_add_sums_cache_tokens() -> None:
    # Given: two usage records that each report cache read/write tokens
    a = LLMUsage(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cache_read_tokens=4,
        cache_write_tokens=2,
    )
    b = LLMUsage(
        prompt_tokens=20,
        completion_tokens=10,
        total_tokens=30,
        cache_read_tokens=1,
        cache_write_tokens=6,
    )

    # When: the two usages are accumulated
    combined = a + b

    # Then: every token bucket, including cache tokens, sums elementwise
    assert combined.prompt_tokens == 30
    assert combined.completion_tokens == 15
    assert combined.total_tokens == 45
    assert combined.cache_read_tokens == 5
    assert combined.cache_write_tokens == 8
