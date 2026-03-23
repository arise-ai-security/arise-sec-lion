"""SEC-bench domain context inference from free-form task text."""

import re
from dataclasses import dataclass

from plugins.security.cve_instance import CVEInstance


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
        return bool(self.repo and self.base_commit and len(self.base_commit) == 40)


class CVEInstanceInferenceService:
    """Extract a SEC-bench CVEInstance from task text using regex patterns."""

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
        "memory": re.compile(r"MemorySanitizer|MSAN|uninitialized", re.IGNORECASE),
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
        cve_match = self.CVE_PATTERN.search(task_description)
        commit_match = self.COMMIT_PATTERN.search(task_description)
        repo_match = self.REPO_PATTERN.search(task_description)

        repo = repo_match.group(1).rstrip(".git") if repo_match else None

        sanitizer = "address"
        for san_type, pattern in self.SANITIZER_PATTERNS.items():
            if pattern.search(task_description):
                sanitizer = san_type
                break

        lang: str | None = None
        for lang_type, pattern in self.LANG_PATTERNS.items():
            if pattern.search(task_description):
                lang = lang_type
                break

        return ExtractionResult(
            cve_id=cve_match.group(0).lower() if cve_match else None,
            repo=repo,
            base_commit=commit_match.group(0) if commit_match else None,
            sanitizer=sanitizer,
            project_name=repo.split("/")[-1] if repo else None,
            lang=lang,
        )

    def infer_instance(
        self,
        task_description: str,
        fail_fast: bool = True,
    ) -> CVEInstance | None:
        result = self.extract(task_description)

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

        if not result.project_name:
            raise CVEInferenceError(["project_name (derived from repo)"])

        if not result.cve_id:
            raise CVEInferenceError(["cve_id (e.g., CVE-2023-1234)"])

        instance_id = f"{result.project_name}.{result.cve_id}"

        return CVEInstance(
            instance_id=instance_id,
            repo=result.repo,  # type: ignore[arg-type]
            project_name=result.project_name,
            lang=result.lang or "",
            work_dir="/src",
            sanitizer=result.sanitizer,
            bug_description="",
            base_commit=result.base_commit,  # type: ignore[arg-type]
        )
