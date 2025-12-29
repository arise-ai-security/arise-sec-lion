"""Task registry API routes.

Provides endpoints for viewing registered tasks (deduplication registry).
"""

from uuid import UUID

from fastapi import APIRouter

from query.api.dependencies import TaskRegistryDep
from query.api.schemas import RegisteredTaskListSchema, RegisteredTaskSchema

router = APIRouter()


@router.get("/{root_id}", response_model=RegisteredTaskListSchema)
async def get_registered_tasks(
    root_id: UUID,
    task_registry: TaskRegistryDep,
) -> RegisteredTaskListSchema:
    """Get all registered tasks for a root agent (execution run).

    This endpoint provides visibility into the task deduplication registry,
    showing all tasks that have been registered during task decomposition.

    Args:
        root_id: Root agent UUID (BOSS agent ID).

    Returns:
        List of registered tasks with their keys and descriptions.
    """
    tasks = await task_registry.get_all_for_root(root_id)

    return RegisteredTaskListSchema(
        tasks=[
            RegisteredTaskSchema(
                task_key=task.task_key,
                task_description=task.task_description,
                registered_by=str(task.registered_by),
                parent_id=str(task.parent_id) if task.parent_id else None,
            )
            for task in tasks
        ],
        total=len(tasks),
    )
