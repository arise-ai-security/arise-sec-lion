"""Tests for adaptive-treatment activation at composition."""

from bootstrap.composition import _is_adaptive_treatment


def test_only_declared_adaptive_treatments_activate_policy() -> None:
    # Given/When/Then: Both adaptive variants activate; unrelated versions do not
    assert _is_adaptive_treatment("b4-adaptive-v1")
    assert _is_adaptive_treatment("b4-adaptive-rolefused-v1")
    assert not _is_adaptive_treatment("b4-full-v0")
    assert not _is_adaptive_treatment(None)
