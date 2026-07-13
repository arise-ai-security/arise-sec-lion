"""Cache breakpoint marker and pure helpers for cache-optimized prompts.

The marker is inserted by the prompt builder's cache-optimized layout between
the static prefix and the variable tail. LLM adapters split on it to set a
provider cache breakpoint; non-adapter consumers strip it so it never reaches
a model or an event trace. Stdlib-only, no heavy imports.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Cache breakpoint marker
# ---------------------------------------------------------------------------
# When the cache-optimized layout is enabled, the prompt builder inserts this
# sentinel between the static prefix (system + role + operation templates) and
# the variable tail (task-specific domain content). LLM adapters split on this
# marker to set a provider cache breakpoint; if no adapter handles it, the
# marker must be stripped before the prompt reaches the model.
CACHE_BREAKPOINT_MARKER = "<<<ARISE_CACHE_BREAKPOINT>>>"


def remove_cache_breakpoints(text: str) -> str:
    """Remove every breakpoint marker from a text segment (no-op if absent).

    Used by the prompt builder to scrub any marker that data-controlled content
    (domain text, task, briefing) might literally contain, so the only marker in
    the assembled prompt is the single seam the builder inserts itself.
    """
    return text.replace(CACHE_BREAKPOINT_MARKER, "")


def split_cache_breakpoint(text: str) -> tuple[str, str] | None:
    """Split a cache-optimized prompt into (static_prefix, variable_tail).

    Returns None when no marker is present. The first marker is the seam; any
    further sentinels on either side (e.g. one embedded in user/task/briefing
    text) are removed so the marker can never survive into the request or trace.
    """
    if CACHE_BREAKPOINT_MARKER not in text:
        return None
    before, _, after = text.partition(CACHE_BREAKPOINT_MARKER)
    before = before.replace(CACHE_BREAKPOINT_MARKER, "")
    after = after.replace(CACHE_BREAKPOINT_MARKER, "")
    return before.rstrip(), after.lstrip()


def strip_cache_breakpoint(text: str) -> str:
    """Remove the marker, rejoining static+variable as plain text (no-op if absent)."""
    split = split_cache_breakpoint(text)
    if split is None:
        return text
    static, variable = split
    return f"{static}\n\n{variable}" if variable else static
