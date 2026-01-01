"""WorkerReport value object for cross-sibling learning (Design Choice 5)."""

from pydantic import BaseModel


class WorkerReport(BaseModel):
    """Structured report from a completed worker for sibling context sharing.

    Enables cross-sibling learning by providing rich context about:
    - What task was assigned and how it was approached
    - What was discovered during execution
    - What was produced (deliverables)
    - Evidence of task fulfillment

    This report is included in WorkCompleted events and shared with
    subsequent sibling workers to inform their execution.
    """

    model_config = {"frozen": True}

    # Task context
    original_task: str = ""  # The task assigned to this worker

    # Execution details
    approach: str = ""  # How the task was executed
    observations: str = ""  # What was discovered during execution

    # Outcomes
    deliverables: str = ""  # What was produced
    fulfillment_evidence: str = ""  # How expectations were met

    @classmethod
    def from_execution(
        cls,
        original_task: str,
        approach: str = "",
        observations: str = "",
        deliverables: str = "",
        fulfillment_evidence: str = "",
    ) -> "WorkerReport":
        """Create a WorkerReport from execution details."""
        return cls(
            original_task=original_task,
            approach=approach,
            observations=observations,
            deliverables=deliverables,
            fulfillment_evidence=fulfillment_evidence,
        )
