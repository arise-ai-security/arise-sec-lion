"""WorkerReport value object for cross-sibling learning (Design Choice 5)."""

from pydantic import BaseModel


class WorkerReport(BaseModel):
    """Structured report from a completed worker for sibling context sharing.

    Enables cross-sibling learning by providing rich context about:
    - What task was assigned and how it was approached
    - What was discovered during execution
    - Challenges encountered during execution
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
    reasoning: str = ""  # Worker's reasoning for chosen approach
    observations: str = ""  # What was discovered during execution
    challenges_encountered: str = ""  # Difficulties faced during execution

    # Outcomes
    deliverables: str = ""  # What was produced
    fulfillment_evidence: str = ""  # How expectations were met
    work_analysis: str = ""  # Analysis of work performed

    @classmethod
    def from_execution(
        cls,
        original_task: str,
        approach: str = "",
        reasoning: str = "",
        observations: str = "",
        challenges_encountered: str = "",
        deliverables: str = "",
        fulfillment_evidence: str = "",
        work_analysis: str = "",
    ) -> "WorkerReport":
        """Create a WorkerReport from execution details."""
        return cls(
            original_task=original_task,
            approach=approach,
            reasoning=reasoning,
            observations=observations,
            challenges_encountered=challenges_encountered,
            deliverables=deliverables,
            fulfillment_evidence=fulfillment_evidence,
            work_analysis=work_analysis,
        )
