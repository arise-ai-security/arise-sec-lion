"""Security domain plugin implementation."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.domain.values.prompt_trace import SectionProvenance
from core.ports.domain_plugin_port import (
    DomainPlugin,
    PreparedRunWorkspace,
    WorkerExecutionContext,
)
from plugins.security.cve_inference import CVEInstanceInferenceService
from plugins.security.decomposition_policy import SecBenchInitialDecompositionPolicy
from plugins.security.cve_instance import CVEInstance
from plugins.security.decomposition_validator import SecBenchDecompositionValidator
from plugins.security.docker_runtime import DockerProcedureSession
from plugins.security.image_resolver import resolve_secbench_image
from plugins.security.procedures import ProcedureInfrastructureError, SecBenchProcedureExecutor
from plugins.security.prompt_strategy import SecBenchPromptStrategy


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.services import PromptStrategy
    from core.domain.values.json_types import JsonObject
    from core.ports.decomposition_validator_port import DecompositionValidator
    from core.ports.procedure_ports import ProcedureExecutorPort
    from plugins.security.container_runtime import (
        SecBenchContainerSession,
        SecBenchWorkspace,
        SecurityContainerRuntime,
    )
    from plugins.security.procedures import ProcedureSession


logger = logging.getLogger(__name__)


def _as_cve_instance(domain_context: object | None) -> CVEInstance | None:
    return domain_context if isinstance(domain_context, CVEInstance) else None


class SecurityDomainPlugin(DomainPlugin):
    """SEC-bench plugin mounted via the DomainPlugin bridge."""

    def __init__(
        self,
        enabled_tools: list[str] | None = None,
        container_runtime: SecurityContainerRuntime | None = None,
        shared_code_prefix_first: bool = False,
        route_policy_version: str = "secbench-manager-recovery-v1",
    ) -> None:
        self._enabled_tools = enabled_tools or []
        self._container_runtime = container_runtime
        self._shared_code_prefix_first = shared_code_prefix_first
        self._route_policy_version = route_policy_version
        # A run's leaf workers ALWAYS share ONE container (started on the first
        # worker, reused for the rest, NOT stopped per-worker) — reaped at
        # process exit by the PID-labeled cleanup (the matrix runs one run per
        # process). This is the correct default: per-worker containers would
        # destroy each worker's container-local state (installed tools,
        # non-mounted scratch files), so a later worker reading an earlier
        # worker's file would error on a torn-down container. Mounted dirs
        # (/src, /testcase, /work) persist either way via bind mounts.
        self._inference_service = CVEInstanceInferenceService()
        self._workspaces: dict[UUID, SecBenchWorkspace] = {}
        self._sessions: dict[UUID, SecBenchContainerSession] = {}
        self._procedure_sessions: dict[UUID, DockerProcedureSession] = {}
        # Audit §13#9: per-root asyncio.Lock around the
        # `_sessions[root_id]` check-and-set in prepare_worker_execution.
        # With the orchestrator's default `max_concurrent_workers=1` the
        # lock is never contended; raising that knob without the lock
        # would let two sibling workers in the same run race past the
        # check-and-start and create two containers.
        self._session_locks: dict[UUID, asyncio.Lock] = {}

    def set_container_runtime(self, container_runtime: SecurityContainerRuntime | None) -> None:
        """Attach the runtime after bootstrap creates infrastructure."""
        self._container_runtime = container_runtime

    def get_prompt_strategy(self) -> PromptStrategy | None:
        return SecBenchPromptStrategy(
            enabled_tools=self._enabled_tools,
            shared_code_first=self._shared_code_prefix_first,
        )

    def get_decomposition_policy(self) -> SecBenchInitialDecompositionPolicy:
        return SecBenchInitialDecompositionPolicy(self._route_policy_version)

    def get_decomposition_validator(self) -> DecompositionValidator | None:
        return SecBenchDecompositionValidator(policy_version=self._route_policy_version)

    def get_procedure_executor(self) -> ProcedureExecutorPort | None:
        return SecBenchProcedureExecutor(session_resolver=self._resolve_procedure_session)

    def _resolve_procedure_session(self, root_id: UUID) -> ProcedureSession | None:
        """Adapt the run's shared container session for the procedure tier."""
        session = self._sessions.get(root_id)
        if session is None:
            return None
        procedure_session = self._procedure_sessions.get(root_id)
        if procedure_session is None:
            procedure_session = DockerProcedureSession(
                session,
                on_removed=lambda: self._invalidate_removed_session(root_id, session),
            )
            self._procedure_sessions[root_id] = procedure_session
        return procedure_session

    async def _invalidate_removed_session(
        self,
        root_id: UUID,
        session: SecBenchContainerSession,
    ) -> None:
        """Drop a container only after the runtime confirms its removal."""
        lock = self._session_locks.setdefault(root_id, asyncio.Lock())
        async with lock:
            if self._sessions.get(root_id) is not session:
                return
            self._sessions.pop(root_id, None)
            self._procedure_sessions.pop(root_id, None)

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        cve_file = kwargs.get("context_file") or kwargs.get("cve_file")
        fail_fast = bool(kwargs.get("fail_fast", False))
        if isinstance(cve_file, (str, Path)):
            return CVEInstance.from_json_file(cve_file)
        return self._inference_service.infer_instance(task_text, fail_fast=fail_fast)

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

        return PreparedRunWorkspace(
            working_directory=str(existing.host_root),
            path_aliases=existing.path_aliases(),
            sealed_surface=existing.sealed_surface,
        )

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

        from plugins.security.mcp.security_tools_server import (
            build_stdio_config as build_mcp_stdio_config,
        )

        # Audit §13#9: serialize the check-and-start under a per-root lock
        # so two concurrent prepare_worker_execution calls for the same
        # root_id can't both pass the membership check and race to
        # start_session.
        lock = self._session_locks.setdefault(root_id, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(root_id)
            if existing is not None:
                procedure_session = self._procedure_sessions.get(root_id)
                if procedure_session is not None and not procedure_session.usable:
                    raise ProcedureInfrastructureError(
                        "procedure container is blocked because removal was not confirmed"
                    )
                session = existing  # reuse the run's one shared container
            else:
                session = await self._container_runtime.start_session(
                    cve=cve_instance,
                    workspace=workspace,
                    agent_id=agent_id,
                )
                self._sessions[root_id] = session
                self._procedure_sessions.pop(root_id, None)

        return WorkerExecutionContext(
            working_directory=str(workspace.host_root),
            task_context={
                "container_session": session.to_task_context(),
                "mcp_servers": {
                    "security_tools": build_mcp_stdio_config(
                        container_id=session.container_id,
                        helper_script=str(workspace.helper_script),
                        work_dir=workspace.container_working_directory,
                        host_source_dir=str(workspace.host_source_dir),
                        host_testcase_dir=str(workspace.host_testcase_dir),
                        host_work_root=str(workspace.host_work_root),
                        workspace_root=str(workspace.host_root),
                        container_source_dir=workspace.container_source_dir,
                        container_testcase_dir=workspace.container_testcase_dir,
                        container_work_dir=workspace.container_work_dir,
                        container_workspace_root=workspace.container_workspace_root,
                    ),
                },
            },
        )

    async def cleanup_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        domain_context: object | None,
    ) -> None:
        # The run's one shared container is kept alive across ALL workers and
        # reaped at process exit by the PID-labeled cleanup (matrix = one run per
        # process). Stopping it per-worker would destroy the container-local
        # state (installed tools, non-mounted files) the next worker needs, so
        # per-worker cleanup is intentionally a no-op.
        _ = (root_id, agent_id, domain_context)
