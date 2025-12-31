"""Task deduplication steps for pipeline execution.

These steps handle task registry operations for avoiding duplicate work.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineContext, StepResult
from core.domain.services import TaskKeyGenerator

if TYPE_CHECKING:
    from core.ports.task_registry_port import TaskRegistryPort

logger = logging.getLogger(__name__)


class FetchRegisteredTasks:
    """Fetch registered tasks for soft guidance in prompt.

    Queries TaskRegistryPort to get already-registered tasks,
    which helps the LLM avoid generating duplicates.
    """

    def __init__(self, task_registry_port: TaskRegistryPort) -> None:
        """Initialize with task registry port.

        Args:
            task_registry_port: Port for task deduplication operations
        """
        self._task_registry_port = task_registry_port

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Fetch registered tasks for the execution run."""
        if ctx.root_id is None:
            # No root_id means no execution context, skip
            return StepResult.ok(ctx)

        registered_tasks = await self._task_registry_port.get_all_for_root(ctx.root_id)
        return StepResult.ok(ctx.with_registered_tasks(registered_tasks))


class DeduplicateSubtasks:
    """Deduplicate subtasks via CQRS projection table.

    Uses TaskRegistryPort to atomically register new tasks.
    Duplicates are filtered out (already being worked on).
    """

    def __init__(self, task_registry_port: TaskRegistryPort) -> None:
        """Initialize with task registry port.

        Args:
            task_registry_port: Port for task deduplication operations
        """
        self._task_registry_port = task_registry_port

    async def execute(self, ctx: PipelineContext) -> StepResult:
        """Filter duplicate subtasks and register new ones."""
        if ctx.root_id is None:
            # No root_id means no execution context, skip deduplication
            return StepResult.ok(ctx)

        if ctx.subtasks is None:
            return StepResult.fail("No subtasks in context to deduplicate")

        agent = ctx.agent
        kept = []

        for subtask in ctx.subtasks:
            task_key = TaskKeyGenerator.generate_key(subtask.description)

            # Atomic check-and-register via projection table with advisory lock
            registered = await self._task_registry_port.register_if_not_exists(
                root_id=ctx.root_id,
                task_key=task_key,
                task_description=subtask.description,
                registered_by=agent.agent_id,
                parent_id=agent.parent_id,
            )

            if registered:
                kept.append(subtask)
            else:
                logger.info(
                    "Skipping duplicate task: agent=%s key=%s desc=%s",
                    agent.agent_id,
                    task_key,
                    subtask.description[:50],
                )

        if len(kept) < len(ctx.subtasks):
            logger.info(
                "Deduplication: kept %d/%d subtasks for agent=%s",
                len(kept),
                len(ctx.subtasks),
                agent.agent_id,
            )

        return StepResult.ok(ctx.with_subtasks(kept))
