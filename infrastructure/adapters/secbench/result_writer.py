"""JSONL result writer for SEC-bench evaluation results.

Writes results in SecVerifier-compatible format:
{instance_id, result: {builder, exploiter, fixer}}
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PhaseResult:
    """Result of a single SEC-bench phase (builder/exploiter/fixer)."""

    passed: bool
    exit_code: int
    duration_seconds: float
    error_message: str | None = None
    sanitizer_triggered: bool | None = None  # For exploiter/fixer phases
    output_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class SecBenchResult:
    """Complete SEC-bench evaluation result for an instance."""

    instance_id: str
    builder: PhaseResult | None = None
    exploiter: PhaseResult | None = None
    fixer: PhaseResult | None = None
    container_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_jsonl_record(self) -> dict[str, Any]:
        """Convert to JSONL record format."""
        result = {}
        if self.builder:
            result["builder"] = self.builder.to_dict()
        if self.exploiter:
            result["exploiter"] = self.exploiter.to_dict()
        if self.fixer:
            result["fixer"] = self.fixer.to_dict()

        record = {
            "instance_id": self.instance_id,
            "result": result,
        }

        if self.container_id:
            record["container_id"] = self.container_id
        if self.metadata:
            record["metadata"] = self.metadata

        return record


class SecBenchResultWriter:
    """Writes SEC-bench results in JSONL format (SecVerifier compatible).

    Each line in the output file is a complete JSON object:
    {"instance_id": "njs.cve-2022-32414", "result": {...}}
    """

    def __init__(self, output_directory: Path):
        """Initialize result writer.

        Args:
            output_directory: Directory where JSONL files will be written.
        """
        self._output_dir = output_directory
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def default_output_path(self) -> Path:
        """Default output file path."""
        return self._output_dir / "secbench_results.jsonl"

    def write_result(
        self,
        result: SecBenchResult,
        output_path: Path | None = None,
    ) -> None:
        """Append result to JSONL file.

        Args:
            result: SEC-bench evaluation result to write.
            output_path: Optional custom output path. Uses default if None.
        """
        path = output_path or self.default_output_path
        record = result.to_jsonl_record()

        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def write_results(
        self,
        results: list[SecBenchResult],
        output_path: Path | None = None,
    ) -> None:
        """Write multiple results to JSONL file.

        Args:
            results: List of SEC-bench evaluation results.
            output_path: Optional custom output path.
        """
        for result in results:
            self.write_result(result, output_path)

    def read_results(self, input_path: Path | None = None) -> list[SecBenchResult]:
        """Read results from JSONL file.

        Args:
            input_path: Path to JSONL file. Uses default if None.

        Returns:
            List of SecBenchResult objects.
        """
        path = input_path or self.default_output_path
        results = []

        if not path.exists():
            return results

        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                results.append(self._parse_record(record))

        return results

    def _parse_record(self, record: dict[str, Any]) -> SecBenchResult:
        """Parse JSONL record into SecBenchResult."""
        result_data = record.get("result", {})

        return SecBenchResult(
            instance_id=record["instance_id"],
            builder=self._parse_phase(result_data.get("builder")),
            exploiter=self._parse_phase(result_data.get("exploiter")),
            fixer=self._parse_phase(result_data.get("fixer")),
            container_id=record.get("container_id"),
            metadata=record.get("metadata", {}),
        )

    def _parse_phase(self, data: dict[str, Any] | None) -> PhaseResult | None:
        """Parse phase result data."""
        if not data:
            return None
        return PhaseResult(
            passed=data["passed"],
            exit_code=data["exit_code"],
            duration_seconds=data["duration_seconds"],
            error_message=data.get("error_message"),
            sanitizer_triggered=data.get("sanitizer_triggered"),
            output_summary=data.get("output_summary"),
        )
