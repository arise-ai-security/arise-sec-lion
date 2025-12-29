"""CVE Instance value object for SEC-bench integration.

Represents a SEC-bench CVE instance with all fields needed for
vulnerability reproduction and benchmarking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CVEInstance:
    """Immutable CVE instance from SEC-bench dataset.

    Maps to SEC-bench schema with fields for vulnerability reproduction.
    All fields are immutable to ensure consistency during benchmark execution.
    """

    instance_id: str  # e.g., "gpac.cve-2023-2838"
    repo: str  # e.g., "gpac/gpac"
    project_name: str  # e.g., "gpac"
    lang: str  # e.g., "c++"
    work_dir: str  # e.g., "/src/gpac"
    sanitizer: str  # "address" | "memory" | "undefined"
    bug_description: str  # Vulnerability description
    base_commit: str  # Git commit hash (40 chars)
    build_sh: str  # Build script content
    secb_sh: str  # SEC-bench helper script
    dockerfile: str  # Docker environment setup
    patch: str  # Gold patch (unified diff format)
    exit_code: int  # Expected exit code after patched execution
    sanitizer_report: str  # Expected sanitizer output
    bug_report: str  # Original GitHub issue/bug report

    @property
    def cve_id(self) -> str:
        """Extract CVE ID from instance_id (e.g., 'cve-2023-2838')."""
        parts = self.instance_id.split(".")
        return parts[1] if len(parts) > 1 else self.instance_id

    @property
    def docker_image(self) -> str:
        """Pre-built Docker image name on DockerHub."""
        return f"hwiwonlee/secb.eval.x86_64.{self.project_name}.{self.cve_id}"

    @property
    def expected_sanitizer_error(self) -> str:
        """Extract expected sanitizer error type from report.

        Parses the sanitizer_report to determine the expected error pattern.
        """
        if "AddressSanitizer" in self.sanitizer_report:
            for error_type in [
                "heap-buffer-overflow",
                "stack-buffer-overflow",
                "heap-use-after-free",
                "stack-use-after-free",
                "global-buffer-overflow",
                "SEGV",
            ]:
                if error_type in self.sanitizer_report:
                    return f"ERROR: AddressSanitizer: {error_type}"
        if "MemorySanitizer" in self.sanitizer_report:
            return "ERROR: MemorySanitizer"
        if "runtime error" in self.sanitizer_report:
            return "runtime error"
        return f"ERROR: {self.sanitizer.capitalize()}Sanitizer"

    @property
    def has_gold_patch(self) -> bool:
        """Check if a gold (reference) patch is available."""
        return bool(self.patch and self.patch.strip())

    @property
    def has_dockerfile(self) -> bool:
        """Check if a Dockerfile is available for building."""
        return bool(self.dockerfile and self.dockerfile.strip())

    @property
    def has_build_script(self) -> bool:
        """Check if a build script is available."""
        return bool(self.build_sh and self.build_sh.strip())

    def to_dict(self) -> dict[str, Any]:
        """Serialize for event storage."""
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "project_name": self.project_name,
            "lang": self.lang,
            "work_dir": self.work_dir,
            "sanitizer": self.sanitizer,
            "bug_description": self.bug_description,
            "base_commit": self.base_commit,
            "build_sh": self.build_sh,
            "secb_sh": self.secb_sh,
            "dockerfile": self.dockerfile,
            "patch": self.patch,
            "exit_code": self.exit_code,
            "sanitizer_report": self.sanitizer_report,
            "bug_report": self.bug_report,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CVEInstance:
        """Deserialize from dictionary."""
        return cls(
            instance_id=data["instance_id"],
            repo=data["repo"],
            project_name=data["project_name"],
            lang=data["lang"],
            work_dir=data["work_dir"],
            sanitizer=data["sanitizer"],
            bug_description=data["bug_description"],
            base_commit=data["base_commit"],
            build_sh=data.get("build_sh", ""),
            secb_sh=data.get("secb_sh", ""),
            dockerfile=data.get("dockerfile", ""),
            patch=data.get("patch", ""),
            exit_code=data.get("exit_code", 0),
            sanitizer_report=data.get("sanitizer_report", ""),
            bug_report=data.get("bug_report", ""),
        )

    @classmethod
    def from_json_file(cls, path: Path | str) -> CVEInstance:
        """Load from JSON file.

        Args:
            path: Path to SEC-bench CVE instance JSON file.

        Returns:
            CVEInstance loaded from the file.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            json.JSONDecodeError: If the file is not valid JSON.
            KeyError: If required fields are missing.
        """
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def to_template_context(self) -> dict[str, Any]:
        """Build context dict for Jinja2 template injection.

        Returns a dictionary suitable for passing to template rendering,
        with computed properties included.
        """
        return {
            # Core identifiers
            "cve_id": self.cve_id,
            "instance_id": self.instance_id,
            "repo": self.repo,
            "project_name": self.project_name,
            # Environment
            "lang": self.lang,
            "work_dir": self.work_dir,
            "sanitizer": self.sanitizer,
            "docker_image": self.docker_image,
            "base_commit": self.base_commit,
            # Vulnerability info
            "bug_description": self.bug_description,
            "expected_error": self.expected_sanitizer_error,
            # Availability flags
            "has_gold_patch": self.has_gold_patch,
            "has_dockerfile": self.has_dockerfile,
            "has_build_script": self.has_build_script,
            # Scripts (may be empty)
            "build_sh": self.build_sh,
            "secb_sh": self.secb_sh,
            "dockerfile": self.dockerfile,
        }
