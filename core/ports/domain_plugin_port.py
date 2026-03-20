"""Bridge port for optional domain-specific behavior."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from core.domain.values.json_types import JsonObject
from core.domain.values.prompt_trace import SectionProvenance

if TYPE_CHECKING:
    from core.application.services.prompt_strategy import PromptStrategy


class DomainPlugin(Protocol):
    """Optional domain-specific behavior injected from bootstrap."""

    def infer_context(self, task_text: str, **kwargs: object) -> object | None:
        """Infer or load opaque domain context for a run."""
        ...

    def create_prompt_strategy(
        self,
        chain_factory: Callable[[], object],
    ) -> "PromptStrategy":
        """Create the domain's prompt strategy implementation."""
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
