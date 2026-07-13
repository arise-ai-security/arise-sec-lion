"""Tests for Anthropic-specific prompt-cache message transformations."""

from infrastructure.adapters.anthropic_cache import apply_anthropic_cache_to_tail


def test_anthropic_tail_gets_cache_control() -> None:
    """The last message receives an ephemeral cache breakpoint for Anthropic."""
    # Given: a conversation ending in a tool result.
    messages = [
        {"role": "user", "content": "p"},
        {"role": "tool", "tool_call_id": "c1", "content": "tool result"},
    ]

    # When: decorating the tail for an Anthropic model.
    result = apply_anthropic_cache_to_tail(messages, "claude-sonnet-4-6")

    # Then: the copied tail receives cache control without mutating the input.
    last = result[-1]["content"]
    assert isinstance(last, list)
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    assert last[-1]["text"] == "tool result"
    assert messages[-1]["content"] == "tool result"


def test_non_anthropic_tail_is_unchanged() -> None:
    """Non-Anthropic models do not receive Anthropic cache metadata."""
    # Given: a conversation ending in a tool result.
    messages = [
        {"role": "user", "content": "p"},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ]

    # When: decorating the tail for a non-Anthropic model.
    result = apply_anthropic_cache_to_tail(messages, "gpt-5.4-mini")

    # Then: the conversation remains unchanged.
    assert result == messages


def test_single_message_tail_is_unchanged() -> None:
    """A single-message conversation has no reusable rolling tail."""
    # Given: a one-message conversation.
    messages = [{"role": "user", "content": "p"}]

    # When: decorating the tail for an Anthropic model.
    result = apply_anthropic_cache_to_tail(messages, "claude-sonnet-4-6")

    # Then: the conversation remains unchanged.
    assert result == messages
