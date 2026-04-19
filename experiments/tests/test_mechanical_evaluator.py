"""Tests for the mechanical evaluator (secb build/repro/patch wrapper)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

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


def test_classify_sanitizer_output_returns_earliest_occurrence() -> None:
    """When multiple classes are present, the one appearing first in the text wins."""
    # Given: output containing both SEGV (earlier) and use-of-uninitialized-value (later)
    output = b"==SEGV at 0xdeadbeef\n... later ==use-of-uninitialized-value detected"

    # When: classified
    cls = classify_sanitizer_output(output)

    # Then: SEGV is returned (it appears first in the text)
    assert cls == "SEGV"


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


def test_evaluate_run_rejects_framed_expected_sanitizer_error(tmp_path: Path) -> None:
    """ValueError is raised when expected_sanitizer_error is a framed string, not a bare class."""
    # Given: a workspace and a framed expected error (as produced by CVEInstance)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # When: evaluate_run is called with framed input / Then: ValueError before any subprocess call
    with pytest.raises(ValueError, match="expected_sanitizer_error"):
        evaluate_run(
            workspace=workspace,
            docker_image="x",
            expected_sanitizer_error="ERROR: AddressSanitizer: heap-buffer-overflow",
        )


def test_evaluate_run_rejects_unknown_sanitizer_class(tmp_path: Path) -> None:
    """ValueError when expected_sanitizer_error is not in the known class list."""
    # Given: a workspace and an unknown error class
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # When/Then: ValueError raised
    with pytest.raises(ValueError, match="expected_sanitizer_error"):
        evaluate_run(
            workspace=workspace,
            docker_image="x",
            expected_sanitizer_error="unknown-error-class",
        )


def test_evaluate_run_rejects_missing_workspace(tmp_path: Path) -> None:
    """FileNotFoundError when workspace doesn't exist (catches relative-path bind-mount footgun)."""
    # Given: a non-existent workspace path (valid expected class so validation passes first)
    # When/Then: FileNotFoundError from resolve(strict=True)
    with pytest.raises(FileNotFoundError):
        evaluate_run(
            workspace=tmp_path / "does-not-exist",
            docker_image="x",
            expected_sanitizer_error="heap-buffer-overflow",
        )


def test_run_with_secb_build_timeout_records_124_exit_code(tmp_path: Path) -> None:
    """A timed-out secb build is recorded with exit code 124 and builder_pass=False."""
    # Given: subprocess.run raises TimeoutExpired on build; repro+patch succeed
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    timeout_exc = subprocess.TimeoutExpired(cmd=["docker"], timeout=300, output=b"", stderr=b"")
    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            timeout_exc,
            MagicMock(returncode=0, stdout=b"", stderr=b""),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]
        # When: evaluated
        result = evaluate_run(
            workspace=workspace,
            docker_image="x",
            expected_sanitizer_error="heap-buffer-overflow",
        )

    # Then: build timeout recorded as exit 124, builder_pass=False
    assert result["details"]["build_exit"] == 124
    assert result["builder_pass"] is False


def test_evaluate_run_surfaces_docker_error_marker(tmp_path: Path) -> None:
    """Docker-level errors (e.g. image not found) are recorded separately from build failure."""
    # Given: secb build returns nonzero with a docker-daemon error in stderr
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            MagicMock(
                returncode=125,
                stdout=b"",
                stderr=b"Unable to find image 'secb-tools:test' locally",
            ),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]
        # When: evaluated
        result = evaluate_run(
            workspace=workspace,
            docker_image="secb-tools:test",
            expected_sanitizer_error="heap-buffer-overflow",
        )

    # Then: builder still fails, but docker_error marker is surfaced for triage
    assert result["builder_pass"] is False
    assert result["details"]["build_docker_error"] == "Unable to find image"


def test_run_secb_includes_network_none(tmp_path: Path) -> None:
    """docker run receives --network=none to match the experiment's isolation invariant."""
    # Given: a workspace and stub responses for all three phases
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("experiments.mechanical_evaluator.subprocess.run") as run_mock:
        run_mock.side_effect = [
            MagicMock(returncode=0, stdout=b"", stderr=b""),
            MagicMock(returncode=0, stdout=b"", stderr=b"==ASan==heap-buffer-overflow..."),
            MagicMock(returncode=0, stdout=b"", stderr=b""),
        ]
        # When: evaluated
        evaluate_run(
            workspace=workspace,
            docker_image="x",
            expected_sanitizer_error="heap-buffer-overflow",
        )

    # Then: every docker invocation included --network=none
    for call in run_mock.call_args_list:
        args = call.args[0]
        assert "--network=none" in args
