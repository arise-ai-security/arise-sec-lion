"""Tests for the mechanical evaluator (secb build/repro/patch wrapper)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from experiments.mechanical_evaluator import classify_sanitizer_output, evaluate_run


if TYPE_CHECKING:
    from pathlib import Path


def test_evaluate_run_records_all_three_phase_outcomes(tmp_path: Path) -> None:
    """All three phases pass when secb exits 0 and exploiter sanitizer error class matches."""
    # Given: a workspace dir with stub testcase files + mocked secb subprocess
    workspace = tmp_path / "workspace"
    (workspace / "testcase").mkdir(parents=True)
    (workspace / "testcase" / "repro.sh").write_text("#!/bin/bash\nexit 0")
    (workspace / "testcase" / "model_patch.diff").write_text("")

    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            MagicMock(returncode=0, stdout=b"", stderr=b"==ASan==heap-buffer-overflow..."),
            MagicMock(returncode=0, stdout=b"", stderr=b"==ASan==heap-buffer-overflow..."),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]

        # When: evaluate_run is called
        result = evaluate_run(
            workspace=workspace,
            docker_image="secb-tools:test",
            expected_sanitizer_error="heap-buffer-overflow",
        )

    # Then: all three phases report pass
    assert result["builder_pass"] is True
    assert result["exploiter_pass"] is True
    assert result["fixer_pass"] is True
    assert result["end_to_end_pass"] is True


def test_exploiter_fails_when_sanitizer_error_class_mismatches(tmp_path: Path) -> None:
    """Exploiter does NOT pass if sanitizer-error class differs from expected."""
    # Given: build OK, repro produces WRONG sanitizer error class
    workspace = tmp_path / "workspace"
    (workspace / "testcase").mkdir(parents=True)
    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            MagicMock(returncode=0, stdout=b"", stderr=b""),
            MagicMock(returncode=1, stdout=b"", stderr=b"==MSan==use-of-uninitialized-value"),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]
        # When: evaluated
        result = evaluate_run(
            workspace=workspace,
            docker_image="secb-tools:test",
            expected_sanitizer_error="heap-buffer-overflow",
        )
    # Then: builder passes, exploiter fails, end-to-end fails
    assert result["builder_pass"] is True
    assert result["exploiter_pass"] is False
    assert result["end_to_end_pass"] is False


def test_classify_sanitizer_output_returns_first_match() -> None:
    """classify_sanitizer_output returns the known class for distinct error types."""
    # Given: bytes output containing one known sanitizer error class
    heap_ubof = b"==ASan==ERROR: AddressSanitizer: heap-buffer-overflow on address..."
    segv = b"received signal SIGSEGV (SEGV)"
    uaf = b"==ASan==ERROR: AddressSanitizer: heap-use-after-free detected"

    # When: classified
    heap_class = classify_sanitizer_output(heap_ubof)
    segv_class = classify_sanitizer_output(segv)
    uaf_class = classify_sanitizer_output(uaf)

    # Then: each returns the matching class
    assert heap_class == "heap-buffer-overflow"
    assert segv_class is not None
    assert segv_class.lower() == "segv"
    assert uaf_class == "heap-use-after-free"


def test_classify_sanitizer_output_returns_none_for_no_match() -> None:
    """classify_sanitizer_output returns None when no known class appears."""
    # Given: bytes output with no sanitizer error class
    output = b"build succeeded\nall tests passed\n"

    # When: classified
    result = classify_sanitizer_output(output)

    # Then: None
    assert result is None


def test_builder_fails_when_secb_build_exits_nonzero(tmp_path: Path) -> None:
    """builder_pass is False when secb build exits non-zero."""
    # Given: secb build exits 2 (compile failure)
    workspace = tmp_path / "workspace"
    (workspace / "testcase").mkdir(parents=True)
    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            MagicMock(returncode=2, stdout=b"", stderr=b"error: build failed"),
            MagicMock(returncode=0, stdout=b"", stderr=b"==ASan==heap-buffer-overflow..."),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]

        # When: evaluated
        result = evaluate_run(
            workspace=workspace,
            docker_image="secb-tools:test",
            expected_sanitizer_error="heap-buffer-overflow",
        )

    # Then: builder fails, end-to-end fails
    assert result["builder_pass"] is False
    assert result["end_to_end_pass"] is False
    assert result["details"]["build_exit"] == 2


def test_fixer_fails_when_secb_patch_exits_nonzero(tmp_path: Path) -> None:
    """fixer_pass is False when secb patch exits non-zero."""
    # Given: build+repro OK but patch exits non-zero
    workspace = tmp_path / "workspace"
    (workspace / "testcase").mkdir(parents=True)
    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            MagicMock(returncode=0, stdout=b"", stderr=b""),
            MagicMock(returncode=0, stdout=b"", stderr=b"==ASan==heap-buffer-overflow..."),
            MagicMock(returncode=1, stdout=b"", stderr=b"patch still triggers sanitizer"),
        ]

        # When: evaluated
        result = evaluate_run(
            workspace=workspace,
            docker_image="secb-tools:test",
            expected_sanitizer_error="heap-buffer-overflow",
        )

    # Then: fixer fails, end-to-end fails
    assert result["builder_pass"] is True
    assert result["exploiter_pass"] is True
    assert result["fixer_pass"] is False
    assert result["end_to_end_pass"] is False
