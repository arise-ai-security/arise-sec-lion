"""Benchmark result value objects for SEC-bench integration.

Tracks success/failure of each benchmark stage (Builder, Exploiter, Fixer)
and provides aggregation for overall benchmark success determination.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class StageResult:
    """Result of a single benchmark stage.

    Captures the outcome of a Builder, Exploiter, or Fixer stage
    with details for debugging and reporting.
    """

    stage: str  # "builder" | "exploiter" | "fixer"
    success: bool
    details: str  # Success message or error description
    sanitizer_output: str | None = None  # For exploiter/fixer stages
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "stage": self.stage,
            "success": self.success,
            "details": self.details,
            "sanitizer_output": self.sanitizer_output,
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageResult:
        """Deserialize from dictionary."""
        return cls(
            stage=data["stage"],
            success=data["success"],
            details=data.get("details", ""),
            sanitizer_output=data.get("sanitizer_output"),
            duration_seconds=data.get("duration_seconds", 0.0),
        )

    @classmethod
    def success_result(
        cls,
        stage: str,
        details: str,
        sanitizer_output: str | None = None,
        duration_seconds: float = 0.0,
    ) -> StageResult:
        """Create a successful stage result."""
        return cls(
            stage=stage,
            success=True,
            details=details,
            sanitizer_output=sanitizer_output,
            duration_seconds=duration_seconds,
        )

    @classmethod
    def failure_result(
        cls,
        stage: str,
        details: str,
        sanitizer_output: str | None = None,
        duration_seconds: float = 0.0,
    ) -> StageResult:
        """Create a failed stage result."""
        return cls(
            stage=stage,
            success=False,
            details=details,
            sanitizer_output=sanitizer_output,
            duration_seconds=duration_seconds,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Aggregated result for a CVE benchmark run.

    Combines results from all three stages (Builder, Exploiter, Fixer)
    to determine overall benchmark success.
    """

    instance_id: str
    builder: StageResult | None = None
    exploiter: StageResult | None = None
    fixer: StageResult | None = None
    total_cost_usd: float = 0.0

    @property
    def overall_success(self) -> bool:
        """True if all three stages succeeded.

        A benchmark is considered successful only if:
        1. Builder stage succeeded (environment set up)
        2. Exploiter stage succeeded (PoC triggers sanitizer)
        3. Fixer stage succeeded (patch fixes vulnerability)
        """
        return (
            self.builder is not None
            and self.builder.success
            and self.exploiter is not None
            and self.exploiter.success
            and self.fixer is not None
            and self.fixer.success
        )

    @property
    def total_duration_seconds(self) -> float:
        """Sum of all stage durations."""
        total = 0.0
        if self.builder:
            total += self.builder.duration_seconds
        if self.exploiter:
            total += self.exploiter.duration_seconds
        if self.fixer:
            total += self.fixer.duration_seconds
        return total

    @property
    def stages_completed(self) -> int:
        """Count of stages that have completed (success or failure)."""
        count = 0
        if self.builder is not None:
            count += 1
        if self.exploiter is not None:
            count += 1
        if self.fixer is not None:
            count += 1
        return count

    @property
    def is_complete(self) -> bool:
        """True if all three stages have completed."""
        return self.stages_completed == 3

    def with_stage_result(self, stage_result: StageResult) -> BenchmarkResult:
        """Return new BenchmarkResult with the given stage result added."""
        if stage_result.stage == "builder":
            return replace(self, builder=stage_result)
        elif stage_result.stage == "exploiter":
            return replace(self, exploiter=stage_result)
        elif stage_result.stage == "fixer":
            return replace(self, fixer=stage_result)
        else:
            raise ValueError(f"Unknown stage: {stage_result.stage}")

    def to_dict(self) -> dict[str, Any]:
        """Serialize for storage."""
        return {
            "instance_id": self.instance_id,
            "builder": self.builder.to_dict() if self.builder else None,
            "exploiter": self.exploiter.to_dict() if self.exploiter else None,
            "fixer": self.fixer.to_dict() if self.fixer else None,
            "total_cost_usd": self.total_cost_usd,
            "overall_success": self.overall_success,
            "total_duration_seconds": self.total_duration_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BenchmarkResult:
        """Deserialize from dictionary."""
        return cls(
            instance_id=data["instance_id"],
            builder=StageResult.from_dict(data["builder"]) if data.get("builder") else None,
            exploiter=StageResult.from_dict(data["exploiter"]) if data.get("exploiter") else None,
            fixer=StageResult.from_dict(data["fixer"]) if data.get("fixer") else None,
            total_cost_usd=data.get("total_cost_usd", 0.0),
        )

    def to_report(self) -> str:
        """Generate human-readable benchmark report."""
        lines = [
            "=== SEC-bench Benchmark Result ===",
            "",
            f"Instance: {self.instance_id}",
            "",
            "Stage Results:",
        ]

        # Builder
        if self.builder:
            status = "[+]" if self.builder.success else "[x]"
            lines.append(
                f"  {status} Builder:   {self.builder.details} ({self.builder.duration_seconds:.1f}s)"
            )
        else:
            lines.append("  [ ] Builder:   Not started")

        # Exploiter
        if self.exploiter:
            status = "[+]" if self.exploiter.success else "[x]"
            lines.append(
                f"  {status} Exploiter: {self.exploiter.details} ({self.exploiter.duration_seconds:.1f}s)"
            )
        else:
            lines.append("  [ ] Exploiter: Not started")

        # Fixer
        if self.fixer:
            status = "[+]" if self.fixer.success else "[x]"
            lines.append(
                f"  {status} Fixer:     {self.fixer.details} ({self.fixer.duration_seconds:.1f}s)"
            )
        else:
            lines.append("  [ ] Fixer:     Not started")

        # Overall
        lines.append("")
        if self.is_complete:
            overall = "SUCCESS" if self.overall_success else "FAILED"
            symbol = "+" if self.overall_success else "x"
            lines.append(f"Overall: {overall} [{symbol}]")
        else:
            lines.append(f"Overall: IN PROGRESS ({self.stages_completed}/3 stages)")

        lines.append("")
        lines.append(f"Total Duration: {self.total_duration_seconds:.1f}s")
        lines.append(f"Total Cost: ${self.total_cost_usd:.2f}")

        return "\n".join(lines)
