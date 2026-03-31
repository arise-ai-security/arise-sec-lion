"""Tests for repository architecture boundary checks."""

from scripts.check_architecture_boundaries import find_boundary_violations


def test_repository_has_no_boundary_violations() -> None:
    assert find_boundary_violations() == []
