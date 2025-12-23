"""Base utilities for worker adapters (DRY extraction)."""

from pathlib import Path
from typing import Any
from uuid import UUID


def validate_task_context(task_context: dict[str, Any]) -> tuple[str, UUID, str]:
    """Validate and extract required fields from task context.

    Args:
        task_context: Dict containing task_description, session_id, and optional working_directory.

    Returns:
        Tuple of (task_description, session_id, working_directory).

    Raises:
        ValueError: If required fields are missing.
    """
    task_description = task_context.get("task_description")
    session_id = task_context.get("session_id")

    if not task_description:
        raise ValueError("task_context must include 'task_description'")
    if not session_id:
        raise ValueError("task_context must include 'session_id'")

    working_dir = task_context.get("working_directory", str(Path.cwd()))

    return task_description, session_id, working_dir
