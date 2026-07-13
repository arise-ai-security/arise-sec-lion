"""SEC-bench CVE instance value object."""

import json
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, computed_field


# Fields retained on CVEInstance for provenance/evaluation but never exposed
# through the prompt-safe context. A full bug report may contain the upstream
# fix commit or exact patch, so it is construction/evaluation data rather than
# a solver input. Evaluation code accesses retained fields through typed
# attributes instead.
_PROMPT_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {"bug_report", "patch", "candidate_fixes", "secb_sh"}
)


class CVEInstance(BaseModel):
    """Immutable CVE instance from SEC-bench dataset."""

    model_config = {"frozen": True}

    instance_id: str
    repo: str
    project_name: str
    lang: str
    work_dir: str
    sanitizer: str
    bug_description: str
    base_commit: str
    build_sh: str = ""
    secb_sh: str = ""
    dockerfile: str = ""
    patch: str = ""
    exit_code: int = 0
    sanitizer_report: str = ""
    bug_report: str = ""
    candidate_fixes: str = ""
    docker_image_override: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cve_id(self) -> str:
        parts = self.instance_id.split(".")
        return parts[1] if len(parts) > 1 else self.instance_id

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docker_image(self) -> str:
        if self.docker_image_override:
            return self.docker_image_override
        return f"hwiwonlee/secb.eval.x86_64.{self.project_name}.{self.cve_id}:patch"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expected_sanitizer_error(self) -> str:
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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_dockerfile(self) -> bool:
        return bool(self.dockerfile and self.dockerfile.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_build_script(self) -> bool:
        return bool(self.build_sh and self.build_sh.strip())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_cve_data(self) -> bool:
        return bool(self.bug_description.strip())

    @classmethod
    def from_json_file(cls, path: Path | str) -> Self:
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.model_validate(data)

    def to_template_context(self) -> dict[str, Any]:
        """Render-safe view for prompt templates.

        Excludes :data:`_PROMPT_FORBIDDEN_FIELDS` so answer-bearing reports,
        patches, candidate fixes, and golden harness content cannot leak into
        prompts. Evaluation-side code that needs retained metadata uses typed
        attributes directly.
        """
        return {
            key: value
            for key, value in self.model_dump().items()
            if key not in _PROMPT_FORBIDDEN_FIELDS
        }
