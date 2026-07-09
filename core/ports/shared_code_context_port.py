"""Shared code-context port: capture viewed source and serve the cached block.

Backs the shared code-prefix cache (spec
docs/superpowers/specs/2026-06-10-shared-code-prefix-cache-design.md). Core
depends only on this Protocol; the concrete provider persists captured file
contents as events and rebuilds the byte-stable block from them. Capture is
driven by real LLM tool calls — the port never decides what to read.
"""

from typing import Protocol
from uuid import UUID


class SharedCodeContextPort(Protocol):
    """Records viewed/edited source files and serves the canonical code block.

    The block is derived by replaying the run's SourceFileObserved /
    SourceFileEdited events, so the exact bytes injected into any worker's
    prompt are recoverable from the event store alone.
    """

    async def record_view(self, root_id: UUID, agent_id: UUID, path: str, content: str) -> None:
        """Record the content a worker's view tool returned (tool RESULT)."""
        ...

    async def record_edit(self, root_id: UUID, agent_id: UUID, path: str) -> None:
        """Mark a file edited; its recorded content is invalidated until re-viewed."""
        ...

    async def code_block(self, root_id: UUID) -> str | None:
        """The canonical byte-stable block for the run, rebuilt from events.

        Returns None when no source has been observed for the run.
        """
        ...

    async def code_index(self, root_id: UUID) -> str | None:
        """Compact index (path, revision, freshness) of the block's files.

        Rendered into the volatile prompt tail near the task. Returns None when
        indexing is disabled or no source has been observed for the run.
        """
        ...
