"""Task deduplication steps for pipeline execution.

These steps handle task registry operations for avoiding duplicate work.
"""



import logging
from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.services import TaskKeyGenerator

if TYPE_CHECKING:
    from core.ports.task_registry_port import TaskRegistryPort

logger = logging.getLogger(__name__)


class FetchRegisteredTasks:
    """Fetch registered tasks for soft guidance in prompt.

    Queries TaskRegistryPort to get already-registered tasks,
    which helps the LLM avoid generating duplicates.
    """

    def __init__(self, task_registry_port: "TaskRegistryPort") -> None:
        """Initialize with task registry port.

        Args:
            task_registry_port: Port for task deduplication operations
        """
        self._task_registry_port = task_registry_port

    async def execute(self, state: PipelineState) -> StepResult:
        """Fetch registered tasks for the execution run."""
        if state.root_id is None:
            # No root_id means no execution context, skip
            return StepResult.ok(state)

        registered_tasks = await self._task_registry_port.get_all_for_root(state.root_id)
        return StepResult.ok(state.with_registered_tasks(registered_tasks))


class DeduplicateSubtasks:
    """Deduplicate subtasks via CQRS projection table.

    Uses TaskRegistryPort to atomically register new tasks.
    Duplicates are filtered out (already being worked on).
    """

    def __init__(self, task_registry_port: "TaskRegistryPort") -> None:
        """Initialize with task registry port.

        Args:
            task_registry_port: Port for task deduplication operations
        """
        self._task_registry_port = task_registry_port

    async def execute(self, state: PipelineState) -> StepResult:
        """Filter duplicate subtasks and register new ones."""
        if state.root_id is None:
            # No root_id means no execution context, skip deduplication
            return StepResult.ok(state)

        if state.subtasks is None:
            return StepResult.fail("No subtasks in context to deduplicate")

        agent = state.agent
        kept = []

        for subtask in state.subtasks:
            task_key = TaskKeyGenerator.generate_key(subtask.description)

            # Atomic check-and-register via projection table with advisory lock
            registered = await self._task_registry_port.register_if_not_exists(
                root_id=state.root_id,
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

        if len(kept) < len(state.subtasks):
            logger.info(
                "Deduplication: kept %d/%d subtasks for agent=%s",
                len(kept),
                len(state.subtasks),
                agent.agent_id,
            )

        return StepResult.ok(state.with_subtasks(kept))
