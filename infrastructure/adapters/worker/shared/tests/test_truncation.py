"""Tests for shared truncation helpers used by worker adapters."""

from infrastructure.adapters.worker.shared.truncation import (
    THOUGHT_CONTENT_CAP,
    TRUNCATION_MARKER,
    cap_thought_content,
)


def test_cap_thought_content_under_cap_returns_unchanged() -> None:
    """Short content passes through untouched with was_truncated=False."""
    # Given: content well under the cap
    raw = "short content"

    # When: capped
    capped, was_truncated, original = cap_thought_content(raw)

    # Then: returns unchanged, flags False, original length reported
    assert capped == raw
    assert was_truncated is False
    assert original == len(raw)


def test_cap_thought_content_over_cap_truncates_with_marker() -> None:
    """Long content is capped at THOUGHT_CONTENT_CAP chars with trailing marker."""
    # Given: content well over the cap
    raw = "x" * (THOUGHT_CONTENT_CAP + 5_000)

    # When: capped
    capped, was_truncated, original = cap_thought_content(raw)

    # Then: cap + marker, was_truncated=True, original length preserved
    assert capped.startswith("x" * THOUGHT_CONTENT_CAP)
    assert capped.endswith(TRUNCATION_MARKER)
    assert was_truncated is True
    assert original == THOUGHT_CONTENT_CAP + 5_000


def test_cap_thought_content_exactly_at_cap_not_truncated() -> None:
    """Content exactly at the cap is not marked as truncated."""
    # Given: content at exact cap length
    raw = "x" * THOUGHT_CONTENT_CAP

    # When: capped
    capped, was_truncated, original = cap_thought_content(raw)

    # Then: unchanged
    assert capped == raw
    assert was_truncated is False
    assert original == THOUGHT_CONTENT_CAP
