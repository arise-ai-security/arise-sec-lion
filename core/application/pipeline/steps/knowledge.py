"""Knowledge context injection steps for pipeline execution (Design Choice 5).

This module provides steps for injecting coworker knowledge into worker prompts.

Note: Knowledge PUBLISHING is handled by ParentNotificationService, NOT here.
Workers do not directly publish to shared context - their parent thinker
reviews and approves the knowledge before publishing (see parent_notifier.py).
"""

from typing import TYPE_CHECKING

from core.application.pipeline.context import PipelineState, StepResult
from core.application.services.context_composer import ContextComposer
from core.domain.values.context import (
    CoworkerKnowledge,
    CoworkerKnowledgeEntry,
)

if TYPE_CHECKING:
    from core.ports.shared_context_port import SharedContextPort


class InjectCoworkerKnowledge:
    """Inject coworker knowledge context into worker prompts (Design Choice 5).

    This step fetches published knowledge from the SharedExecutionContext
    and injects it into the worker's prompt via the ContextComposer.

    Workers can then learn from earlier workers' discoveries, avoiding
    redundant work like re-discovering file locations or trigger conditions.

    This step should run before prompt building steps (BuildWorkerPrompt)
    so the context is available for template rendering.

    Note: Workers do NOT publish knowledge. Only parent thinkers can publish
    after reviewing worker reports. See ParentNotificationService.
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
        # Get root_id from hierarchy limits
        root_id = state.root_id
        if root_id is None:
            # No root_id means no shared context available
            return StepResult.ok(state)

        # Fetch shared context
        shared_context = await self._shared_context_port.get(root_id)
        if shared_context is None:
            return StepResult.ok(state)

        # Get all published knowledge
        all_knowledge = shared_context.get_all_knowledge()
        if not all_knowledge:
            return StepResult.ok(state)

        # Convert to CoworkerKnowledgeEntry objects
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

        # Get or create context composer and add coworker knowledge
        composer = state.context_composer or ContextComposer()
        composer.add(CoworkerKnowledge(entries=entries))

        return StepResult.ok(state.with_context_composer(composer))
