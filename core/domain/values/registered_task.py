"""Value object for task registry deduplication."""

from uuid import UUID

from pydantic import BaseModel


class RegisteredTask(BaseModel):
    """Registered task for deduplication."""

    model_config = {"frozen": True}

    task_key: str
    task_description: str
    registered_by: UUID
    parent_id: UUID | None
