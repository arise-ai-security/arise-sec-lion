"""Context entry for cross-session knowledge sharing."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from core.domain.subtask import WorkerReport


class ContextEntry(BaseModel):
    """Immutable context entry for cross-session knowledge sharing.

    Published by supervisors when workers complete tasks. Other workers
    can query the context dashboard to find relevant entries and learn
    from previous work.
    """

    model_config = {"frozen": True}

    # Entry ID (assigned when stored in the dashboard)
    entry_id: UUID | None = Field(None, description="Entry UUID in the dashboard")

    # Key: supervisor-generated descriptive title for the work
    work_title: str = Field(
        ...,
        min_length=1,
        description="Descriptive title for the work (used as key)",
    )

    # Supervisor context (from SubtaskJustification)
    objective: str = Field(..., description="What this subtask aimed to achieve")
    justification: str = Field(..., description="Why this task was assigned")

    # Worker analysis (synthesized from WorkerReport)
    work_analysis: str = Field(
        ...,
        description="Analysis of how the work was executed",
    )

    # Original WorkerReport (preserved for detailed context)
    worker_report: WorkerReport

    # Metadata
    session_id: UUID = Field(description="Root (BOSS) session ID for scoping")
    worker_id: UUID = Field(description="Worker agent that completed this task")
    created_at: datetime

    # Tags for improved relevance matching
    tags: list[str] = Field(
        default_factory=list,
        description="Keywords extracted from task for filtering",
    )
