"""Knowledge context and report steps for pipeline execution (Design Choice 5).

This module provides steps for:
1. Injecting coworker knowledge into worker prompts (reading)
2. Generating worker reports after execution
3. Thinker review and publishing to shared context

Design Choice 5 Flow:
  Worker completes → GenerateWorkerReport → WorkCompleted event (with report)
  Parent receives → ThinkerReviewAndPublish → KnowledgePublished event
  Later workers → InjectCoworkerKnowledge → reads from SharedExecutionContext
"""

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.application.services.context_composer import ContextComposer
from core.domain.values.context import (
    CoworkerKnowledge,
    CoworkerKnowledgeEntry,
)
from core.domain.values.worker_report import WorkerReport

if TYPE_CHECKING:
    from core.ports.shared_context_port import SharedContextPort


# =============================================================================
# Worker Pipeline Steps (Reading Knowledge)
# =============================================================================


class InjectCoworkerKnowledge:
    """Inject coworker knowledge context into worker prompts (Design Choice 5).

    This step fetches published knowledge from the SharedExecutionContext
    and injects it into the worker's prompt via the ContextComposer.

    Workers can then learn from earlier workers' discoveries, avoiding
    redundant work like re-discovering file locations or trigger conditions.

    This step should run before prompt building steps (BuildWorkerPrompt)
    so the context is available for template rendering.
    """

    def __init__(self, shared_context_port: "SharedContextPort") -> None:
        """Initialize with shared context port.

        Args:
            shared_context_port: Port for accessing shared execution context.
        """
        self._shared_context_port = shared_context_port

    async def execute(self, state: PipelineState) -> StepResult:
        """Inject coworker knowledge into pipeline state context composer.

        Fetches all published knowledge from SharedExecutionContext and
        converts it to CoworkerKnowledge for template rendering.
        """
        root_id = state.root_id
        if root_id is None:
            return StepResult.ok(state)

        shared_context = await self._shared_context_port.get(root_id)
        if shared_context is None:
            return StepResult.ok(state)

        all_knowledge = shared_context.get_all_knowledge()
        if not all_knowledge:
            return StepResult.ok(state)

        entries = tuple(
            CoworkerKnowledgeEntry(
                key=k.key,
                objective=k.objective,
                relevance=k.relevance,
                key_findings=k.key_findings,
                deliverables=k.deliverables,
                source_worker_id=str(k.source_worker_id),
                published_by=str(k.published_by),
            )
            for k in all_knowledge
        )

        composer = state.context_composer or ContextComposer()
        composer.add(CoworkerKnowledge(entries=entries))

        return StepResult.ok(state.with_context_composer(composer))


# =============================================================================
# Worker Report Pipeline Steps (Generating Report)
# =============================================================================


class GenerateWorkerReport:
    """Generate a structured WorkerReport after worker execution (Design Choice 5).

    This step extracts key information from the worker's execution and creates
    a structured WorkerReport that will be included in the WorkCompleted event.

    The report is then sent to the parent thinker for review and potential
    publishing to the shared context.

    This step should run after RunWorkerSession completes.
    """

    async def execute(self, state: PipelineState) -> StepResult:
        """Generate WorkerReport from worker execution results.

        Extracts approach, observations, deliverables, and fulfillment evidence
        from the worker's result and attaches it to the agent for inclusion
        in the WorkCompleted event.
        """
        agent = state.agent

        # Only generate report if worker has a result
        if agent.result is None:
            return StepResult.ok(state)

        # Extract thinker justification for alignment check
        thinker_objective = ""
        if state.context_composer:
            ctx = state.context_composer.build()
            if "thinker_justification" in ctx:
                thinker_objective = ctx.get("thinker_justification", {}).get(
                    "objective", ""
                )

        # Generate structured report
        report = WorkerReport.from_execution(
            original_task=agent.task_description or "",
            approach=_extract_approach(agent.result),
            observations=_extract_observations(agent.result),
            challenges_encountered=_extract_challenges(agent.result),
            deliverables=_extract_deliverables(agent.result),
            fulfillment_evidence=_build_fulfillment_evidence(
                agent.result, thinker_objective
            ),
        )

        # Attach report to agent for inclusion in WorkCompleted event
        agent.pending_worker_report = report

        return StepResult.ok(state)


def _extract_approach(result: str) -> str:
    """Extract approach description from worker result."""
    # Simple extraction: first 200 chars summarizing the approach
    if len(result) <= 200:
        return result
    return result[:200] + "..."


def _extract_observations(result: str) -> str:
    """Extract key observations from worker result."""
    # Look for common patterns indicating observations
    observations = []

    # Check for file paths mentioned
    import re

    file_patterns = re.findall(r"[\w/]+\.\w+:\d+", result)
    if file_patterns:
        observations.append(f"Referenced files: {', '.join(file_patterns[:5])}")

    # Check for discovered values
    if "found" in result.lower() or "discovered" in result.lower():
        observations.append("Made discoveries during execution")

    return "; ".join(observations) if observations else ""


def _extract_challenges(result: str) -> str:
    """Extract challenges encountered from worker result."""
    # Look for common patterns indicating challenges or difficulties
    challenges = []
    result_lower = result.lower()

    # Check for error patterns
    if "error" in result_lower or "failed" in result_lower:
        challenges.append("Encountered errors during execution")

    # Check for difficulty indicators
    if "difficult" in result_lower or "challenging" in result_lower:
        challenges.append("Faced difficulties")

    # Check for workaround patterns
    if "workaround" in result_lower or "instead" in result_lower:
        challenges.append("Required workarounds")

    # Check for retry/attempt patterns
    if "retry" in result_lower or "attempt" in result_lower:
        challenges.append("Multiple attempts needed")

    return "; ".join(challenges) if challenges else ""


def _extract_deliverables(result: str) -> str:
    """Extract deliverables from worker result."""
    # Look for created/modified files
    import re

    created_patterns = re.findall(r"(?:created|wrote|generated)\s+(\S+)", result.lower())
    if created_patterns:
        return f"Created: {', '.join(created_patterns[:5])}"
    return ""


def _build_fulfillment_evidence(result: str, objective: str) -> str:
    """Build evidence that worker fulfilled supervisor expectations."""
    if not objective:
        return "Task completed"

    # Simple check if objective keywords appear in result
    objective_words = set(objective.lower().split())
    result_words = set(result.lower().split())
    overlap = objective_words & result_words

    if len(overlap) > 3:
        return f"Objective '{objective[:50]}...' addressed"
    return "Task completed"


# =============================================================================
# Thinker Review Pipeline Steps (Publishing Knowledge)
# =============================================================================


class ThinkerReviewAndPublish:
    """Thinker reviews worker report and publishes to shared context (Design Choice 5).

    This step is called when a parent thinker receives a completed worker child.
    The thinker reviews the worker's report and decides what to publish to the
    global SharedExecutionContext for other workers to learn from.

    This enforces the design principle: workers do NOT publish directly.
    Only parent thinkers can approve and publish knowledge.
    """

    def __init__(self, shared_context_port: "SharedContextPort") -> None:
        """Initialize with shared context port.

        Args:
            shared_context_port: Port for accessing shared execution context.
        """
        self._shared_context_port = shared_context_port

    async def execute(
        self,
        child_agent_id: str,
        child_task: str,
        child_result: str,
        child_report: WorkerReport | None,
        parent_agent_id: str,
        root_id: str,
    ) -> bool:
        """Review worker report and publish curated knowledge.

        Args:
            child_agent_id: ID of the completed worker child.
            child_task: The task the worker was assigned.
            child_result: The worker's result.
            child_report: Optional structured WorkerReport.
            parent_agent_id: ID of the parent thinker (publisher).
            root_id: Root agent ID for shared context lookup.

        Returns:
            True if knowledge was published, False otherwise.
        """
        from uuid import UUID

        # Fetch shared context
        shared_context = await self._shared_context_port.get(UUID(root_id))
        if shared_context is None:
            return False

        # Review: Check if report is valuable enough to publish
        if not self._is_valuable(child_result, child_report):
            return False

        # Build key findings from report and result
        key_findings = []
        if child_report:
            if child_report.observations:
                key_findings.append(child_report.observations)
            if child_report.deliverables:
                key_findings.append(child_report.deliverables)
        if not key_findings and child_result:
            key_findings.append(child_result[:500])

        # Determine relevance
        relevance = f"Completed: {child_task[:80]}..."

        # Parent thinker publishes (approves) the knowledge
        task_key = child_task[:100] if child_task else ""
        if not task_key:
            return False

        current_version = shared_context.version
        shared_context.publish_knowledge(
            key=task_key,
            objective=child_task,
            relevance=relevance,
            key_findings=key_findings,
            deliverables=[child_report.deliverables] if child_report and child_report.deliverables else [],
            source_worker_id=UUID(child_agent_id),
            published_by=UUID(parent_agent_id),
        )

        # Persist if there are changes
        if shared_context.events:
            await self._shared_context_port.save(
                shared_context, expected_version=current_version
            )
            shared_context.mark_changes_as_committed()

        return True

    def _is_valuable(
        self,
        result: str,
        report: WorkerReport | None,
    ) -> bool:
        """Determine if worker report contains valuable insights worth publishing.

        Quality gate: Only publish meaningful knowledge, not noise.
        """
        # Must have some result
        if not result:
            return False

        # Must have substantial result (not just "done" or similar)
        if len(result) < 50:
            return False

        # If we have a report, check for observations or deliverables
        if report:
            if report.observations or report.deliverables:
                return True

        # Check result for indicators of valuable content
        valuable_indicators = [
            "file:",
            "line:",
            "found",
            "discovered",
            "created",
            "modified",
            "error",
            "success",
        ]
        result_lower = result.lower()
        return any(indicator in result_lower for indicator in valuable_indicators)
