"""Bridge port for optional domain-specific behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.values.json_types import JsonObject
from core.domain.values.prompt_trace import SectionProvenance


if TYPE_CHECKING:
    from core.application.services import PromptStrategy


@dataclass(frozen=True)
class WorkspacePathAlias:
    """Run-local mapping from a domain path to a host workspace path."""

    virtual_path: str
    host_path: str


@dataclass(frozen=True)
class PreparedRunWorkspace:
    """Optional workspace override returned by a domain plugin."""

    working_directory: str
    path_aliases: tuple[WorkspacePathAlias, ...] = ()


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Optional worker runtime data returned by a domain plugin."""

    working_directory: str | None = None
    task_context: dict[str, Any] = field(default_factory=dict)


class DomainPlugin(Protocol):
    """Optional domain-specific behavior injected from bootstrap."""

    def infer_context(self, task_text: str, **kwargs: object) -> object | None: ...

    def get_run_metadata(self, domain_context: object) -> JsonObject: ...

    def get_tag_mappings(self) -> dict[str, SectionProvenance]: ...

    def get_provenance_patterns(self) -> list[tuple[str, SectionProvenance]]: ...

    async def prepare_run(
        self,
        *,
        root_id: UUID,
        run_output_path: Path,
        domain_context: object | None,
    ) -> PreparedRunWorkspace | None: ...

    async def prepare_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        run_output_path: Path,
        domain_context: object | None,
    ) -> WorkerExecutionContext | None: ...

    async def cleanup_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        domain_context: object | None,
    ) -> None: ...

    def get_prompt_strategy(self) -> PromptStrategy | None: ...
