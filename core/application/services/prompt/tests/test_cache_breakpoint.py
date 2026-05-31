"""Tests for the cache breakpoint marker helpers."""

from __future__ import annotations

from core.application.services.prompt.cache_breakpoint import (
    CACHE_BREAKPOINT_MARKER,
    remove_cache_breakpoints,
    split_cache_breakpoint,
    strip_cache_breakpoint,
)


def test_remove_cache_breakpoints_strips_all_occurrences() -> None:
    # Given: a segment containing several stray markers (e.g. injected via data)
    text = f"a{CACHE_BREAKPOINT_MARKER}b{CACHE_BREAKPOINT_MARKER}c"
    # When: removing breakpoints
    result = remove_cache_breakpoints(text)
    # Then: every marker is gone, surrounding content preserved
    assert CACHE_BREAKPOINT_MARKER not in result
    assert result == "abc"


def test_remove_cache_breakpoints_is_noop_without_marker() -> None:
    # Given/When/Then: text without a marker is returned unchanged
    assert remove_cache_breakpoints("plain text") == "plain text"


def test_split_returns_none_without_marker() -> None:
    # Given: a plain prompt with no cache breakpoint marker
    text = "just a plain prompt"
    # When: the prompt is split on the marker
    result = split_cache_breakpoint(text)
    # Then: there is nothing to split
    assert result is None


def test_split_returns_static_and_variable_trimmed() -> None:
    # Given: a prompt with whitespace surrounding the marker
    text = f"static prefix  \n\n{CACHE_BREAKPOINT_MARKER}\n\n  variable tail"
    # When: the prompt is split on the marker
    result = split_cache_breakpoint(text)
    # Then: the static prefix and variable tail are returned without surrounding whitespace
    assert result == ("static prefix", "variable tail")


def test_strip_removes_marker() -> None:
    # Given: a prompt containing the marker between static and variable parts
    text = f"static{CACHE_BREAKPOINT_MARKER}variable"
    # When: the marker is stripped
    result = strip_cache_breakpoint(text)
    # Then: the marker is gone and the parts are rejoined as plain text
    assert CACHE_BREAKPOINT_MARKER not in result
    assert result == "static\n\nvariable"


def test_strip_is_noop_without_marker() -> None:
    # Given: a prompt with no marker
    text = "unchanged prompt"
    # When: the marker is stripped
    result = strip_cache_breakpoint(text)
    # Then: the prompt is returned unchanged
    assert result == text


def test_round_trip_from_builder_style_string() -> None:
    # Given: a builder-style cache-optimized prompt
    static = "system + role + operation"
    variable = "task-specific domain content"
    text = f"{static}\n\n{CACHE_BREAKPOINT_MARKER}\n\n{variable}"
    # When: the prompt is split and then stripped
    split = split_cache_breakpoint(text)
    stripped = strip_cache_breakpoint(text)
    # Then: split recovers the original parts and strip rejoins them without the marker
    assert split == (static, variable)
    assert stripped == f"{static}\n\n{variable}"
    assert CACHE_BREAKPOINT_MARKER not in stripped


def test_split_strips_repeated_sentinel_in_variable_tail() -> None:
    # Given: the real seam plus a second sentinel echoed in the variable tail
    # (e.g. user/task/briefing text that happens to contain the marker)
    text = (
        f"STATIC\n\n{CACHE_BREAKPOINT_MARKER}\n\n"
        f"task mentions {CACHE_BREAKPOINT_MARKER} verbatim"
    )
    # When: the prompt is split and stripped
    split = split_cache_breakpoint(text)
    stripped = strip_cache_breakpoint(text)
    # Then: no marker survives on either side, so it cannot reach a model or trace
    assert split is not None
    static, variable = split
    assert CACHE_BREAKPOINT_MARKER not in static
    assert CACHE_BREAKPOINT_MARKER not in variable
    assert CACHE_BREAKPOINT_MARKER not in stripped
