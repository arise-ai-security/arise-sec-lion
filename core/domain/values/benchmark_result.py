"""Benchmark result value objects for SEC-bench integration.

Tracks success/failure of each benchmark stage (Builder, Exploiter, Fixer)
and provides aggregation for overall benchmark success determination.
"""



from typing import Self

from pydantic import BaseModel, computed_field


class StageResult(BaseModel):
    """Result of a single benchmark stage.

    Captures the outcome of a Builder, Exploiter, or Fixer stage
    with details for debugging and reporting.
    """

    model_config = {"frozen": True}

    stage: str  # "builder" | "exploiter" | "fixer"
    success: bool
    details: str  # Success message or error description
    sanitizer_output: str | None = None  # For exploiter/fixer stages
    duration_seconds: float = 0.0

    @classmethod
    def success_result(
        cls,
        stage: str,
        details: str,
        sanitizer_output: str | None = None,
        duration_seconds: float = 0.0,
    ) -> Self:
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
    ) -> Self:
        """Create a failed stage result."""
        return cls(
            stage=stage,
            success=False,
            details=details,
            sanitizer_output=sanitizer_output,
            duration_seconds=duration_seconds,
        )


class BenchmarkResult(BaseModel):
    """Aggregated result for a CVE benchmark run.

    Combines results from all three stages (Builder, Exploiter, Fixer)
    to determine overall benchmark success.
    """

    model_config = {"frozen": True}

    instance_id: str
    builder: StageResult | None = None
    exploiter: StageResult | None = None
    fixer: StageResult | None = None
    total_cost_usd: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overall_success(self) -> bool:
        """True if all three stages succeeded."""
        return (
            self.builder is not None
            and self.builder.success
            and self.exploiter is not None
            and self.exploiter.success
            and self.fixer is not None
            and self.fixer.success
        )

    @computed_field  # type: ignore[prop-decorator]
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

    @computed_field  # type: ignore[prop-decorator]
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_complete(self) -> bool:
        """True if all three stages have completed."""
        return self.stages_completed == 3

    def with_stage_result(self, stage_result: StageResult) -> Self:
        """Return new BenchmarkResult with the given stage result added."""
        if stage_result.stage == "builder":
            return self.model_copy(update={"builder": stage_result})
        elif stage_result.stage == "exploiter":
            return self.model_copy(update={"exploiter": stage_result})
        elif stage_result.stage == "fixer":
            return self.model_copy(update={"fixer": stage_result})
        else:
            raise ValueError(f"Unknown stage: {stage_result.stage}")

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
