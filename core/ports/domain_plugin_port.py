"""Bridge port for optional domain-specific behavior."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.values.json_types import JsonObject
from core.domain.values.prompt_trace import SectionProvenance

if TYPE_CHECKING:
    from core.application.services.prompt_strategy import PromptStrategy


@dataclass(frozen=True)
class PreparedRunWorkspace:
    """Optional workspace override returned by a domain plugin."""

    working_directory: str


@dataclass(frozen=True)
class WorkerExecutionContext:
    """Optional worker runtime data returned by a domain plugin."""

    working_directory: str | None = None
    task_context: dict[str, Any] = field(default_factory=dict)


class DomainPlugin(Protocol):
    """Optional domain-specific behavior injected from bootstrap."""

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        """Infer or load opaque domain context for a run."""
        ...

    def enrich_prompt(
        self,
        prompt: str,
        *,
        domain_context: object | None,
        briefing: object | None = None,
        chain_factory: Callable[[], object] | None = None,
    ) -> str:
        """Apply optional post-build enrichment to a prompt."""
        ...

    def get_run_metadata(self, domain_context: object) -> JsonObject:
        """Return JSON-safe run metadata persisted on RunStarted."""
        ...

    def get_tag_mappings(self) -> dict[str, SectionProvenance]:
        """Return prompt tag provenance overrides for this domain."""
        ...

    def get_provenance_patterns(self) -> list[tuple[str, SectionProvenance]]:
        """Return prompt tag provenance pattern rules for this domain."""
        ...

    async def prepare_run(
        self,
        *,
        root_id: UUID,
        run_output_path: Path,
        domain_context: object | None,
    ) -> PreparedRunWorkspace | None:
        """Prepare an optional run workspace before the boss agent is created."""
        ...

    async def prepare_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        run_output_path: Path,
        domain_context: object | None,
    ) -> WorkerExecutionContext | None:
        """Prepare optional runtime state before a worker executes."""
        ...

    async def cleanup_worker_execution(
        self,
        *,
        root_id: UUID,
        agent_id: UUID,
        domain_context: object | None,
    ) -> None:
        """Clean up optional runtime state after worker execution."""
        ...

    def get_prompt_strategy(self) -> PromptStrategy | None:
        """Return the prompt strategy paired with this domain plugin, or None."""
        ...
