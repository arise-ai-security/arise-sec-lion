"""Verification service for SEC-bench phases.

Executes and verifies secb build/repro/patch commands as defined in the SEC-bench paper.
"""

import re
from dataclasses import dataclass

from core.ports.secbench_container_port import SecBenchContainerPort, SecbResult

from .result_writer import PhaseResult

# Sanitizer error patterns to detect in output
SANITIZER_PATTERNS = [
    r"AddressSanitizer:",
    r"MemorySanitizer:",
    r"UndefinedBehaviorSanitizer:",
    r"ThreadSanitizer:",
    r"LeakSanitizer:",
    r"ERROR: AddressSanitizer",
    r"ERROR: MemorySanitizer",
    r"SUMMARY: AddressSanitizer",
    r"SUMMARY: MemorySanitizer",
    r"heap-buffer-overflow",
    r"stack-buffer-overflow",
    r"use-after-free",
    r"double-free",
    r"null pointer",
    r"integer overflow",
    r"out-of-bounds",
]


@dataclass
class VerificationResult:
    """Result of a verification phase."""

    phase: str  # "build", "repro", "patch"
    passed: bool
    secb_result: SecbResult
    sanitizer_triggered: bool = False
    expected_error_found: bool = False
    error_message: str | None = None

    def to_phase_result(self) -> PhaseResult:
        """Convert to PhaseResult for JSONL output."""
        return PhaseResult(
            passed=self.passed,
            exit_code=self.secb_result.exit_code,
            duration_seconds=self.secb_result.duration_seconds,
            error_message=self.error_message,
            sanitizer_triggered=self.sanitizer_triggered if self.phase != "build" else None,
            output_summary=self._truncate_output(self.secb_result.output, 500),
        )

    def _truncate_output(self, output: str, max_length: int) -> str:
        """Truncate output for storage."""
        if len(output) <= max_length:
            return output
        return output[:max_length] + "... (truncated)"


class VerificationService:
    """Service for verifying SEC-bench phases.

    Executes secb commands and checks results against expected outcomes
    as defined in the SEC-bench paper.
    """

    def __init__(
        self,
        container_port: SecBenchContainerPort,
        timeout_seconds: int = 600,
    ):
        """Initialize verification service.

        Args:
            container_port: Port for container operations.
            timeout_seconds: Default timeout for secb commands.
        """
        self._container = container_port
        self._timeout = timeout_seconds

    async def verify_build(self, container_id: str) -> VerificationResult:
        """Verify build phase by running secb build.

        Build is successful if:
        - Exit code is 0
        - No compilation errors in output

        Args:
            container_id: Target container ID.

        Returns:
            VerificationResult with build outcome.
        """
        result = await self._container.execute_secb(
            container_id=container_id,
            command="build",
            timeout_seconds=self._timeout,
        )

        passed = result.exit_code == 0
        error_message = None

        if not passed:
            error_message = self._extract_build_error(result.output)

        return VerificationResult(
            phase="build",
            passed=passed,
            secb_result=result,
            error_message=error_message,
        )

    async def verify_repro(
        self,
        container_id: str,
        expected_error: str | None = None,
    ) -> VerificationResult:
        """Verify reproduction phase by running secb repro.

        Reproduction is successful if:
        - Sanitizer error is triggered in output
        - Expected error type matches (if specified)

        Args:
            container_id: Target container ID.
            expected_error: Expected sanitizer error type (e.g., "heap-buffer-overflow").

        Returns:
            VerificationResult with reproduction outcome.
        """
        result = await self._container.execute_secb(
            container_id=container_id,
            command="repro",
            timeout_seconds=self._timeout,
        )

        output = result.output
        sanitizer_triggered = self._detect_sanitizer_error(output)

        # Check if expected error is found (if specified)
        expected_error_found = True
        if expected_error:
            expected_error_found = expected_error.lower() in output.lower()

        # Repro is successful if sanitizer is triggered and expected error is found
        passed = sanitizer_triggered and expected_error_found

        error_message = None
        if not sanitizer_triggered:
            error_message = "No sanitizer error detected in output"
        elif not expected_error_found:
            error_message = f"Expected error '{expected_error}' not found in output"

        return VerificationResult(
            phase="repro",
            passed=passed,
            secb_result=result,
            sanitizer_triggered=sanitizer_triggered,
            expected_error_found=expected_error_found,
            error_message=error_message,
        )

    async def verify_patch(self, container_id: str) -> VerificationResult:
        """Verify patch phase by running secb patch.

        Patch is successful if:
        - Exit code is 0 (or 1 with no sanitizer errors)
        - No sanitizer errors in output after patch applied

        Args:
            container_id: Target container ID.

        Returns:
            VerificationResult with patch outcome.
        """
        result = await self._container.execute_secb(
            container_id=container_id,
            command="patch",
            timeout_seconds=self._timeout,
        )

        output = result.output
        sanitizer_triggered = self._detect_sanitizer_error(output)

        # Patch is successful if no sanitizer errors after applying patch
        # Exit code 1 is acceptable if no sanitizer errors (normal exception handling)
        passed = not sanitizer_triggered and result.exit_code in (0, 1)

        error_message = None
        if sanitizer_triggered:
            error_message = "Sanitizer error still present after patch"
        elif result.exit_code not in (0, 1):
            error_message = f"Patch command failed with exit code {result.exit_code}"

        return VerificationResult(
            phase="patch",
            passed=passed,
            secb_result=result,
            sanitizer_triggered=sanitizer_triggered,
            error_message=error_message,
        )

    def _detect_sanitizer_error(self, output: str) -> bool:
        """Check if output contains sanitizer errors."""
        for pattern in SANITIZER_PATTERNS:
            if re.search(pattern, output, re.IGNORECASE):
                return True
        return False

    def _extract_build_error(self, output: str, max_length: int = 200) -> str:
        """Extract first error message from build output."""
        # Look for common error patterns
        error_patterns = [
            r"error:\s*(.+)",
            r"Error:\s*(.+)",
            r"fatal:\s*(.+)",
            r"FAILED:\s*(.+)",
        ]

        for pattern in error_patterns:
            match = re.search(pattern, output)
            if match:
                error = match.group(1).strip()
                if len(error) > max_length:
                    return error[:max_length] + "..."
                return error

        return "Build failed (check logs for details)"
