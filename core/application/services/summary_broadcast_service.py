"""Worker summary broadcast service.

This module provides a service for workers to broadcast their work
summaries to multiple recipients (parent, boss, or specific ancestors).

The service uses SharedExecutionContext to store broadcasts as artifacts,
enabling recipients to retrieve summaries targeted at them.

Example usage:
    # Worker broadcasts summary
    broadcast_service.broadcast(
        worker_id=worker.agent_id,
        worker_task="Scan auth module",
        summary_text="Found 3 vulnerabilities...",
        recipients=["parent", "boss"],  # Send to both
        context=shared_context,
    )

    # Parent retrieves summaries
    summaries = broadcast_service.get_summaries_for_recipient(
        recipient_id=parent.agent_id,
        recipient_type="parent",
        context=shared_context,
    )
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.values.context import WorkerSummaryBroadcast, WorkerSummaryEntry

if TYPE_CHECKING:
    from core.domain.shared_context import SharedExecutionContext


# Artifact key prefix for broadcast summaries
BROADCAST_ARTIFACT_PREFIX = "worker_summary_broadcast"


class SummaryBroadcastService:
    """Service for broadcasting and retrieving worker summaries.

    This service enables workers to send their work summaries to
    multiple recipients in the hierarchy. Summaries are stored in
    SharedExecutionContext as artifacts.

    The recipient targeting works as follows:
    - "parent": Only immediate parent receives
    - "boss": Only root boss (depth=0) receives
    - "both": Both parent AND boss receive
    - "depth_N": Ancestor at specific depth (e.g., "depth_1")

    Thread Safety:
        Uses SharedExecutionContext which has OCC. Multiple workers
        can broadcast concurrently; conflicts are handled by retry.
    """

    def broadcast(
        self,
        worker_id: UUID,
        worker_task: str,
        summary_text: str,
        recipients: list[str],
        context: "SharedExecutionContext",
        parent_id: UUID | None = None,
    ) -> None:
        """Broadcast a worker summary to specified recipients.

        Stores the summary in SharedExecutionContext as an artifact
        keyed by recipient type. Multiple recipients can be specified.

        Args:
            worker_id: ID of the worker sending the summary.
            worker_task: Description of the task the worker performed.
            summary_text: The summary content to broadcast.
            recipients: List of recipient types ["parent", "boss", "both"].
            context: SharedExecutionContext to store the broadcast.
            parent_id: Parent ID (needed for "parent" recipient type).
        """
        timestamp = datetime.now().isoformat()

        # Expand "both" to individual recipients
        expanded_recipients = []
        for recipient in recipients:
            if recipient == "both":
                expanded_recipients.extend(["parent", "boss"])
            else:
                expanded_recipients.append(recipient)

        # Create broadcast entry
        entry = {
            "worker_id": str(worker_id),
            "worker_task": worker_task,
            "summary_text": summary_text,
            "timestamp": timestamp,
            "parent_id": str(parent_id) if parent_id else None,
        }

        # Store for each recipient type
        for recipient in set(expanded_recipients):  # Dedupe
            artifact_key = f"{BROADCAST_ARTIFACT_PREFIX}:{recipient}:{worker_id}"
            context.store_artifact(
                key=artifact_key,
                content_type="application/json",
                stored_by=worker_id,
                content=json.dumps(entry),
            )

    def get_summaries_for_recipient(
        self,
        recipient_type: str,
        context: "SharedExecutionContext",
        recipient_id: UUID | None = None,
    ) -> WorkerSummaryBroadcast:
        """Retrieve all summaries targeted at a recipient.

        Scans SharedExecutionContext for broadcast artifacts matching
        the recipient type and builds a WorkerSummaryBroadcast.

        Args:
            recipient_type: Type of recipient ("parent", "boss", "depth_N").
            context: SharedExecutionContext to retrieve from.
            recipient_id: Optional specific recipient ID to filter by.

        Returns:
            WorkerSummaryBroadcast containing all matching summaries.
        """
        entries: list[WorkerSummaryEntry] = []

        # Get all artifact keys matching the recipient type
        prefix = f"{BROADCAST_ARTIFACT_PREFIX}:{recipient_type}:"

        for key in context.list_artifacts():
            if key.startswith(prefix):
                artifact = context.get_artifact(key)
                if artifact and artifact.content:
                    try:
                        data = json.loads(artifact.content)
                        entries.append(WorkerSummaryEntry(
                            worker_id=data.get("worker_id", ""),
                            worker_task=data.get("worker_task", ""),
                            summary_text=data.get("summary_text", ""),
                            timestamp=data.get("timestamp", ""),
                        ))
                    except (json.JSONDecodeError, TypeError):
                        continue

        return WorkerSummaryBroadcast(
            recipient_type=recipient_type,
            summaries=tuple(entries),
        )


def broadcast_summary_to_parent_and_boss(
    worker_id: UUID,
    worker_task: str,
    summary_text: str,
    context: "SharedExecutionContext",
    parent_id: UUID | None = None,
) -> None:
    """Convenience function to broadcast summary to both parent and boss.

    This is the most common use case: workers want their summary to
    be visible to their immediate parent AND the root boss.

    Args:
        worker_id: ID of the worker sending the summary.
        worker_task: Description of the task the worker performed.
        summary_text: The summary content to broadcast.
        context: SharedExecutionContext to store the broadcast.
        parent_id: Parent ID for parent recipient filtering.

    Example:
        # After worker completes:
        broadcast_summary_to_parent_and_boss(
            worker_id=agent.agent_id,
            worker_task=agent.task_description,
            summary_text="Successfully exploited SQL injection...",
            context=shared_context,
            parent_id=agent.parent_id,
        )
    """
    service = SummaryBroadcastService()
    service.broadcast(
        worker_id=worker_id,
        worker_task=worker_task,
        summary_text=summary_text,
        recipients=["both"],
        context=context,
        parent_id=parent_id,
    )
