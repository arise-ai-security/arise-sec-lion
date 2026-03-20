"""Security domain plugin implementation."""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from core.domain.values.json_types import JsonObject
from core.domain.values.prompt_trace import SectionProvenance
from plugins.security.cve_inference import CVEInstanceInferenceService
from plugins.security.cve_instance import CVEInstance
from plugins.security.prompt_strategy import SecBenchPromptStrategy, detect_benchmark_branch
from plugins.security.security_tool import get_tools_for_phase

if TYPE_CHECKING:
    from core.application.services.prompt_strategy import PromptStrategy


def _as_cve_instance(domain_context: object | None) -> CVEInstance | None:
    return domain_context if isinstance(domain_context, CVEInstance) else None


class SecurityDomainPlugin:
    """SEC-bench plugin mounted via the DomainPlugin bridge."""

    def __init__(self, enabled_tools: list[str] | None = None) -> None:
        self._enabled_tools = enabled_tools or []
        self._inference_service = CVEInstanceInferenceService()

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        cve_file = kwargs.get("cve_file")
        fail_fast = bool(kwargs.get("fail_fast", False))
        if isinstance(cve_file, (str, Path)):
            return CVEInstance.from_json_file(cve_file)
        return self._inference_service.infer_instance(task_text, fail_fast=fail_fast)

    def create_prompt_strategy(
        self,
        chain_factory: Callable[[], object],
    ) -> "PromptStrategy":
        return SecBenchPromptStrategy(chain_factory)  # type: ignore[arg-type]

    def enrich_prompt(
        self,
        prompt: str,
        *,
        domain_context: object | None,
        briefing: object | None = None,
        chain_factory: Callable[[], object] | None = None,
    ) -> str:
        cve_instance = _as_cve_instance(domain_context)
        if cve_instance is None or not self._enabled_tools or chain_factory is None:
            return prompt

        phase = detect_benchmark_branch(briefing)  # type: ignore[arg-type]
        if not phase:
            return prompt

        tools = get_tools_for_phase(phase, self._enabled_tools)
        if not tools:
            return prompt

        chain = chain_factory()  # type: ignore[assignment]
        enrichment = (
            chain.render(
                "domains/secbench/tools.j2",
                security_tools=[tool.model_dump() for tool in tools],
            ).build()
        )
        return f"{prompt}\n\n{enrichment}"

    def get_run_metadata(self, domain_context: object) -> JsonObject:
        cve_instance = _as_cve_instance(domain_context)
        if cve_instance is None:
            return {}
        return {"instance_id": cve_instance.instance_id}

    def get_tag_mappings(self) -> dict[str, SectionProvenance]:
        return {
            "cve_context": SectionProvenance.SYSTEM,
            "security_context": SectionProvenance.SYSTEM,
            "issue_description": SectionProvenance.SYSTEM,
            "repository_info": SectionProvenance.SYSTEM,
            "cve_instance": SectionProvenance.SYSTEM,
            "bug_report": SectionProvenance.SYSTEM,
            "sanitizer": SectionProvenance.SYSTEM,
            "work_dir": SectionProvenance.SYSTEM,
            "base_commit_hash": SectionProvenance.SYSTEM,
            "commit_hash": SectionProvenance.SYSTEM,
            "commit_hash1": SectionProvenance.SYSTEM,
            "commit_hash2": SectionProvenance.SYSTEM,
            "commit_url": SectionProvenance.SYSTEM,
            "changed_file_path": SectionProvenance.SYSTEM,
            "security_tools": SectionProvenance.SYSTEM,
        }

    def get_provenance_patterns(self) -> list[tuple[str, SectionProvenance]]:
        return [
            (r"cve|security|workspace|instance|commit|file|bug|sanitizer|exploit", SectionProvenance.SYSTEM),
        ]
