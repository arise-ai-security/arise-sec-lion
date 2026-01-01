"""Depth-targeted summary broadcast service.

This module provides a service for workers to broadcast their work
summaries to ancestors at specific hierarchy depths.

Common use case: Workers report to the first child of BOSS (depth=1),
which acts as a coordinator or aggregator.

Example usage:
    # Worker at depth 3 broadcasts summary to depth 1 ancestor
    depth_broadcast_service.broadcast(
        worker_id=worker.agent_id,
        worker_task="Scan auth module",
        summary_text="Found 3 vulnerabilities...",
        target_depth=1,
        source_depth=3,
        context=shared_context,
    )

    # Depth-1 ancestor retrieves summaries
    summaries = depth_broadcast_service.get_summaries_for_depth(
        target_depth=1,
        context=shared_context,
    )
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.values.context import DepthTargetedSummary, DepthTargetedSummaryEntry

if TYPE_CHECKING:
    from core.domain.shared_context import SharedExecutionContext


# Artifact key prefix for depth-targeted broadcasts
DEPTH_BROADCAST_PREFIX = "depth_targeted_summary"


class DepthBroadcastService:
    """Service for broadcasting and retrieving depth-targeted worker summaries.

    This service enables workers to send their work summaries to ancestors
    at specific depths in the hierarchy. Summaries are stored in
    SharedExecutionContext as artifacts keyed by target depth.

    Depth mapping:
    - depth=0: BOSS (root)
    - depth=1: First child of BOSS (common coordinator role)
    - depth=N: Ancestor at that specific depth

    Thread Safety:
        Uses SharedExecutionContext which has OCC. Multiple workers
        can broadcast concurrently; conflicts are handled by retry.
    """

    def broadcast(
        self,
        worker_id: UUID,
        worker_task: str,
        summary_text: str,
        target_depth: int,
        source_depth: int,
        context: "SharedExecutionContext",
    ) -> None:
        """Broadcast a worker summary to a specific ancestor depth.

        Stores the summary in SharedExecutionContext as an artifact
        keyed by target depth and worker ID.

        Args:
            worker_id: ID of the worker sending the summary.
            worker_task: Description of the task the worker performed.
            summary_text: The summary content to broadcast.
            target_depth: Target ancestor depth (0=boss, 1=first child, etc.).
            source_depth: Depth of the worker in the hierarchy.
            context: SharedExecutionContext to store the broadcast.
        """
        timestamp = datetime.now().isoformat()

        entry = {
            "worker_id": str(worker_id),
            "worker_task": worker_task,
            "summary_text": summary_text,
            "source_depth": source_depth,
            "target_depth": target_depth,
            "timestamp": timestamp,
        }

        # Key format: depth_targeted_summary:depth_N:worker_id
        artifact_key = f"{DEPTH_BROADCAST_PREFIX}:depth_{target_depth}:{worker_id}"
        context.store_artifact(
            key=artifact_key,
            content_type="application/json",
            stored_by=worker_id,
            content=json.dumps(entry),
        )

    def get_summaries_for_depth(
        self,
        target_depth: int,
        context: "SharedExecutionContext",
    ) -> DepthTargetedSummary:
        """Retrieve all summaries targeted at a specific depth.

        Scans SharedExecutionContext for broadcast artifacts matching
        the target depth and builds a DepthTargetedSummary.

        Args:
            target_depth: The depth to retrieve summaries for.
            context: SharedExecutionContext to retrieve from.

        Returns:
            DepthTargetedSummary containing all matching summaries.
        """
        entries: list[DepthTargetedSummaryEntry] = []

        # Get all artifact keys matching the target depth
        prefix = f"{DEPTH_BROADCAST_PREFIX}:depth_{target_depth}:"

        for key in context.list_artifacts():
            if key.startswith(prefix):
                artifact = context.get_artifact(key)
                if artifact and artifact.content:
                    try:
                        data = json.loads(artifact.content)
                        entries.append(DepthTargetedSummaryEntry(
                            worker_id=data.get("worker_id", ""),
                            worker_task=data.get("worker_task", ""),
                            summary_text=data.get("summary_text", ""),
                            source_depth=data.get("source_depth", 0),
                            timestamp=data.get("timestamp", ""),
                        ))
                    except (json.JSONDecodeError, TypeError):
                        continue

        return DepthTargetedSummary(
            target_depth=target_depth,
            summaries=tuple(entries),
        )


def broadcast_summary_to_depth1(
    worker_id: UUID,
    worker_task: str,
    summary_text: str,
    source_depth: int,
    context: "SharedExecutionContext",
) -> None:
    """Convenience function to broadcast summary to depth=1 ancestor.

    Depth=1 is the first child of BOSS, commonly used as a coordinator
    or aggregator role. This function provides a simple API for the
    most common depth-targeted use case.

    Args:
        worker_id: ID of the worker sending the summary.
        worker_task: Description of the task the worker performed.
        summary_text: The summary content to broadcast.
        source_depth: Depth of the worker in the hierarchy.
        context: SharedExecutionContext to store the broadcast.

    Example:
        # After worker completes:
        broadcast_summary_to_depth1(
            worker_id=agent.agent_id,
            worker_task=agent.task_description,
            summary_text="Successfully exploited SQL injection...",
            source_depth=agent.depth,  # Worker's depth in hierarchy
            context=shared_context,
        )
    """
    service = DepthBroadcastService()
    service.broadcast(
        worker_id=worker_id,
        worker_task=worker_task,
        summary_text=summary_text,
        target_depth=1,
        source_depth=source_depth,
        context=context,
    )
