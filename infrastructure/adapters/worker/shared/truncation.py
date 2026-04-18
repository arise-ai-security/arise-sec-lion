"""Shared truncation constants and helpers for worker adapters.

Keeping a single source of truth for THOUGHT_CONTENT_CAP and TRUNCATION_MARKER
ensures Claude SDK and OpenHands adapters produce events with identical
truncation semantics -- required for Pillar B cross-adapter analysis.
"""

from __future__ import annotations


THOUGHT_CONTENT_CAP = 10_240
TRUNCATION_MARKER = "…[TRUNCATED]"


def cap_thought_content(raw: str) -> tuple[str, bool, int]:
    """Apply the shared content cap; return (capped, was_truncated, original_bytes).

    Args:
        raw: The original content string (pre-cap).

    Returns:
        (capped_content, was_truncated, original_byte_length).
        If raw is already <= cap, returns (raw, False, len(raw)).
        Otherwise returns (raw[:cap] + marker, True, len(raw)).
    """
    original_bytes = len(raw)
    if original_bytes <= THOUGHT_CONTENT_CAP:
        return raw, False, original_bytes
    return raw[:THOUGHT_CONTENT_CAP] + TRUNCATION_MARKER, True, original_bytes
