"""Worker that bridges the legacy OpenHands adapter to ``WorkerPort``.

The existing ``infrastructure.adapters.worker.openhands_adapter.OpenHandsAdapter``
implements ``WorkerToolPort`` (event-streaming for hierarchical mode). This
adapter wraps it so flat-mode dispatch can call ``run_task`` and receive a
single ``WorkerResult``.

Note: container-session prep performed by ``SecurityDomainPlugin.prepare_worker_execution``
is NOT replayed here. Flat-mode OpenHands runs therefore start with the
generic ``workspace.root`` as the working directory; mirroring the secbench
container lifecycle to flat mode is a follow-up tracked alongside PR 6.

Note: ``tool_policy`` is implicit at the OpenHands SDK level (no argv flags
exposed); ``timeouts`` come from the wrapped adapter's construction-time
config. Both invariants are accepted by ``run_task`` to satisfy the
``WorkerPort`` contract but are not threaded through to the adapter today.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from core.application.run_invariants import WorkerResult
from core.domain.events.events import WorkCompleted, WorkFailed


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.run_invariants import (
        TaskPromptSpec,
        TimeoutBudget,
        ToolPolicy,
        WorkspaceSpec,
    )
    from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter


logger = logging.getLogger(__name__)


class OpenHandsWorker:
    """Adapter satisfying ``WorkerPort`` by delegating to ``OpenHandsAdapter``."""

    def __init__(self, *, adapter: OpenHandsAdapter) -> None:
        self._adapter = adapter

    async def run_task(
        self,
        *,
        run_id: UUID,
        spec: TaskPromptSpec,
        tool_policy: ToolPolicy,
        timeouts: TimeoutBudget,
        workspace: WorkspaceSpec,
    ) -> WorkerResult:
        del tool_policy  # OpenHands' allowlist is implicit; see build_tool_policy.
        del timeouts  # OpenHandsAdapter has its own timeout configured at construction time.

        task_context: dict[str, object] = {
            "agent_id": run_id,
            "task_description": spec.rendered_prompt,
            "tool_name": "openhands",
            "working_directory": str(workspace.root),
        }

        start = time.monotonic()
        exit_status: str = "failed"
        last_completed: str | None = None
        last_failure: str | None = None

        try:
            async for event in self._adapter.run_session(task_context):
                if isinstance(event, WorkCompleted):
                    exit_status = "completed"
                    last_completed = event.result
                elif isinstance(event, WorkFailed):
                    exit_status = "failed"
                    last_failure = event.reason
        except Exception as error:
            logger.exception("OpenHandsWorker.run_task failed")
            wall = time.monotonic() - start
            return WorkerResult(
                run_id=run_id,
                exit_status="failed",
                wall_time_seconds=wall,
                output_summary=f"OpenHandsWorker error: {error!r}",
            )

        wall = time.monotonic() - start
        summary = last_completed if exit_status == "completed" else last_failure
        truncated = summary[:500] if isinstance(summary, str) else None
        return WorkerResult(
            run_id=run_id,
            exit_status=exit_status,  # type: ignore[arg-type]
            wall_time_seconds=wall,
            output_summary=truncated,
        )
