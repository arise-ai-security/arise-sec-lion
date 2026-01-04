"""CVE Instance inference service for extracting CVEInstance from task text.

This service extracts CVEInstance fields from unstructured task descriptions
when --cve-file is not provided. Uses regex-only extraction with fail-fast
behavior for missing required fields.
"""

import re
from dataclasses import dataclass

from core.domain.values.cve_instance import CVEInstance


class CVEInferenceError(Exception):
    """Raised when required fields cannot be extracted from task description."""

    def __init__(self, missing_fields: list[str]) -> None:
        self.missing_fields = missing_fields
        hint = (
            "Provide --cve-file for explicit SEC-bench configuration, "
            "or include GitHub repo URL and 40-char commit hash in task description."
        )
        super().__init__(
            f"Cannot infer CVE instance. Missing: {', '.join(missing_fields)}. {hint}"
        )


@dataclass(frozen=True)
class ExtractionResult:
    """Result of regex-based CVE field extraction."""

    cve_id: str | None
    repo: str | None
    base_commit: str | None
    sanitizer: str
    project_name: str | None
    lang: str | None

    def has_required_fields(self) -> bool:
        """Check if minimum required fields were extracted.

        Required: repo + base_commit (40-char hash).
        """
        return bool(self.repo and self.base_commit and len(self.base_commit) == 40)


class CVEInstanceInferenceService:
    """Extracts CVEInstance from task description using regex patterns.

    This service uses regex-only extraction (no LLM inference) following
    SEC-bench's approach of reliable, deterministic field extraction.

    Required fields (fail-fast if missing):
    - repo: GitHub repository in owner/repo format
    - base_commit: 40-character git commit hash

    Optional fields (use defaults if missing):
    - cve_id: CVE identifier (generated if missing)
    - sanitizer: Detected from keywords (default: "address")
    - lang: Programming language (default: "c")
    - work_dir: Working directory (default: "/src")
    """

    # Regex patterns (following SEC-bench preprocessor/report.py patterns)
    CVE_PATTERN = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)
    COMMIT_PATTERN = re.compile(r"\b[a-f0-9]{40}\b")
    REPO_PATTERN = re.compile(
        r"(?:github\.com[:/]|git@github\.com:)([^/\s]+/[^/\s:]+?)(?:\.git)?(?:\s|$|/|,|;|:)",
        re.IGNORECASE,
    )

    SANITIZER_PATTERNS: dict[str, re.Pattern[str]] = {
        "address": re.compile(
            r"AddressSanitizer|ASAN|heap-buffer-overflow|stack-buffer-overflow|"
            r"use-after-free|heap-use-after-free|global-buffer-overflow|SEGV",
            re.IGNORECASE,
        ),
        "memory": re.compile(
            r"MemorySanitizer|MSAN|uninitialized",
            re.IGNORECASE,
        ),
        "undefined": re.compile(
            r"UndefinedBehaviorSanitizer|UBSAN|runtime error|undefined behavior",
            re.IGNORECASE,
        ),
    }

    LANG_PATTERNS: dict[str, re.Pattern[str]] = {
        "c++": re.compile(r"\.cpp|\.cc|\.cxx|\.hpp|C\+\+|c\+\+", re.IGNORECASE),
        "c": re.compile(r"\.c\b|\.h\b|\bC program\b|\bC code\b|\bwritten in C\b", re.IGNORECASE),
        "python": re.compile(r"\.py\b|\bpython\b", re.IGNORECASE),
        "rust": re.compile(r"\.rs\b|\brust\b", re.IGNORECASE),
        "go": re.compile(r"\.go\b|\bgolang\b", re.IGNORECASE),
        "java": re.compile(r"\.java\b|\bjava\b", re.IGNORECASE),
        "javascript": re.compile(r"\.js\b|\bjavascript\b|\bnode\.?js\b", re.IGNORECASE),
    }

    def extract(self, task_description: str) -> ExtractionResult:
        """Extract CVE fields from task description using regex patterns.

        Args:
            task_description: User-provided task text.

        Returns:
            ExtractionResult with extracted fields.
        """
        # Extract CVE ID
        cve_match = self.CVE_PATTERN.search(task_description)
        cve_id = cve_match.group(0).lower() if cve_match else None

        # Extract 40-char commit hash
        commit_match = self.COMMIT_PATTERN.search(task_description)
        base_commit = commit_match.group(0) if commit_match else None

        # Extract GitHub repo (owner/repo format)
        repo_match = self.REPO_PATTERN.search(task_description)
        repo = repo_match.group(1).rstrip(".git") if repo_match else None

        # Detect sanitizer type from keywords
        sanitizer = "address"  # Default
        for san_type, pattern in self.SANITIZER_PATTERNS.items():
            if pattern.search(task_description):
                sanitizer = san_type
                break

        # Detect programming language
        lang: str | None = None
        for lang_type, pattern in self.LANG_PATTERNS.items():
            if pattern.search(task_description):
                lang = lang_type
                break

        # Derive project name from repo
        project_name = repo.split("/")[-1] if repo else None

        return ExtractionResult(
            cve_id=cve_id,
            repo=repo,
            base_commit=base_commit,
            sanitizer=sanitizer,
            project_name=project_name,
            lang=lang,
        )

    def infer_instance(
        self,
        task_description: str,
        fail_fast: bool = True,
    ) -> CVEInstance | None:
        """Infer CVEInstance from task description.

        Args:
            task_description: User-provided task text.
            fail_fast: If True, raise CVEInferenceError when required fields missing.
                      If False, return None when extraction fails.

        Returns:
            CVEInstance if extraction successful, None if fail_fast=False and failed.

        Raises:
            CVEInferenceError: If fail_fast=True and required fields missing.
        """
        result = self.extract(task_description)

        # Check required fields
        if not result.has_required_fields():
            if fail_fast:
                missing: list[str] = []
                if not result.repo:
                    missing.append("repo (GitHub URL like github.com/owner/repo)")
                if not result.base_commit:
                    missing.append("base_commit (40-char git hash)")
                elif len(result.base_commit) != 40:
                    missing.append(
                        f"base_commit (found {len(result.base_commit)}-char, need 40-char)"
                    )
                raise CVEInferenceError(missing)
            return None

        # Build instance_id in format: {project_name}.{cve_id}
        # Examples: gpac.cve-2021-32437, exiv2.cve-2017-14857
        if not result.project_name:
            # project_name derives from repo, which is required - should not reach here
            raise CVEInferenceError(["project_name (derived from repo)"])

        if not result.cve_id:
            raise CVEInferenceError(["cve_id (e.g., CVE-2023-1234)"])

        instance_id = f"{result.project_name}.{result.cve_id}"

        return CVEInstance(
            instance_id=instance_id,
            repo=result.repo,  # type: ignore[arg-type]  # Validated above
            project_name=result.project_name or result.repo.split("/")[-1],  # type: ignore[union-attr]
            lang=result.lang or "",  # Empty if not detected (won't render in prompt)
            work_dir="/src",  # SEC-bench default
            sanitizer=result.sanitizer,
            bug_description="",  # Empty - no curated CVE data when inferring
            base_commit=result.base_commit,  # type: ignore[arg-type]  # Validated above
        )
