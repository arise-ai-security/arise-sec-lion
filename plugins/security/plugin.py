"""Security domain plugin implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from core.domain.values.prompt_trace import SectionProvenance
from core.ports.domain_plugin_port import (
    DomainPlugin,
    PreparedRunWorkspace,
    WorkerExecutionContext,
)
from plugins.security.cve_inference import CVEInstanceInferenceService
from plugins.security.cve_instance import CVEInstance
from plugins.security.image_resolver import resolve_secbench_image
from plugins.security.prompt_strategy import SecBenchPromptStrategy, detect_benchmark_branch
from plugins.security.security_tool import get_tools_for_phase


if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from core.application.services import PromptStrategy
    from core.application.services.prompt.prompt_builder import TemplateChain
    from core.domain.values.json_types import JsonObject
    from plugins.security.container_runtime import (
        SecBenchContainerSession,
        SecBenchWorkspace,
        SecurityContainerRuntime,
    )


logger = logging.getLogger(__name__)


def _as_cve_instance(domain_context: object | None) -> CVEInstance | None:
    return domain_context if isinstance(domain_context, CVEInstance) else None


class SecurityDomainPlugin(DomainPlugin):
    """SEC-bench plugin mounted via the DomainPlugin bridge."""

    def __init__(
        self,
        enabled_tools: list[str] | None = None,
        container_runtime: SecurityContainerRuntime | None = None,
    ) -> None:
        self._enabled_tools = enabled_tools or []
        self._container_runtime = container_runtime
        self._inference_service = CVEInstanceInferenceService()
        self._workspaces: dict[UUID, SecBenchWorkspace] = {}
        self._sessions: dict[UUID, SecBenchContainerSession] = {}

    def set_container_runtime(
        self, container_runtime: SecurityContainerRuntime | None
    ) -> None:
        """Attach the runtime after bootstrap creates infrastructure."""
        self._container_runtime = container_runtime

    def get_prompt_strategy(self) -> PromptStrategy | None:
        return SecBenchPromptStrategy()

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        cve_file = kwargs.get("context_file") or kwargs.get("cve_file")
        fail_fast = bool(kwargs.get("fail_fast", False))
        if isinstance(cve_file, (str, Path)):
            return CVEInstance.from_json_file(cve_file)
        return self._inference_service.infer_instance(task_text, fail_fast=fail_fast)

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

        chain = cast("TemplateChain", chain_factory())
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
            (
                r"cve|security|workspace|instance|commit|file|bug|sanitizer|exploit",
                SectionProvenance.SYSTEM,
            ),
        ]

    async def prepare_run(
        self,
        *,
        root_id: UUID,
        run_output_path: Path,
        domain_context: object | None,
    ) -> PreparedRunWorkspace | None:
        cve_instance = _as_cve_instance(domain_context)
        if cve_instance is None or self._container_runtime is None:
            return None

        existing = self._workspaces.get(root_id)
        if existing is None:
            image = resolve_secbench_image(
                cve_instance.docker_image,
                security_tools_enabled=bool(self._enabled_tools),
            )
            existing = await self._container_runtime.prepare_workspace(
                cve=cve_instance,
                run_output_path=run_output_path,
                image=image,
                root_id=root_id,
            )
            self._workspaces[root_id] = existing

        return PreparedRunWorkspace(working_directory=str(existing.host_root))

    async def prepare_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        run_output_path: Path,
        domain_context: object | None,
    ) -> WorkerExecutionContext | None:
        cve_instance = _as_cve_instance(domain_context)
        if cve_instance is None or self._container_runtime is None:
            return None

        workspace = self._workspaces.get(root_id)
        if workspace is None:
            prepared = await self.prepare_run(
                root_id=root_id,
                run_output_path=run_output_path,
                domain_context=domain_context,
            )
            if prepared is None:
                return None
            workspace = self._workspaces[root_id]

        if root_id in self._sessions:
            raise RuntimeError(f"SEC-bench container already active for run {root_id}")

        session = await self._container_runtime.start_session(
            cve=cve_instance,
            workspace=workspace,
            agent_id=agent_id,
        )
        self._sessions[root_id] = session

        return WorkerExecutionContext(
            working_directory=str(workspace.host_root),
            task_context={"container_session": session.to_task_context()},
        )

    async def cleanup_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        domain_context: object | None,
    ) -> None:
        _ = agent_id
        _ = domain_context
        session = self._sessions.pop(root_id, None)
        if session is None or self._container_runtime is None:
            return

        try:
            await self._container_runtime.stop_session(session)
        except Exception:
            logger.exception(
                "Failed to clean up SEC-bench container",
                extra={"root_id": str(root_id), "container_id": session.container_id},
            )
