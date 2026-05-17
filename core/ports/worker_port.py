"""Worker port: contract every worker adapter satisfies, regardless of execution mode.

All workers (claude_code, openhands, google_adk, ...) implement this Protocol.
The dispatcher (flat or hierarchical) constructs the invariant value objects via
``core.application.run_invariants`` and passes them through unchanged so flat-mode
baselines and hierarchical-mode workers see byte-identical task framing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkerResult,
        WorkspaceSpec,
    )


@runtime_checkable
class WorkerPort(Protocol):
    """Adapter that runs a single task within an execution context."""

    async def run_task(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
    ) -> WorkerResult: ...
