"""Context value objects for inter-agent communication.

Organized by data flow direction:
- parent_to_child: Parent → Child (spawn-time payload)
- child_to_parent: Child → Parent (task results)
- sibling_to_sibling: Child ↔ Child (worker coordination)
- limits: Execution constraints (hierarchy limits)
- base: ContextData protocol for composable context
- data_types: Concrete context types for ContextComposer

New Composable Context System:
    The ContextComposer + ContextData types provide a flexible,
    programmatic way to compose prompt context. Use these when
    you need custom control over what data each agent sees.

    Example:
        from core.application.services.context_composer import ContextComposer
        from core.domain.values.context import ParentSummary, SiblingResults

        context = (
            ContextComposer()
            .add(ParentSummary(task="...", result="..."))
            .add(SiblingResults(siblings=[...]))
        )
        template_vars = context.build()
"""

from core.domain.values.context.base import ContextData
from core.domain.values.context.child_to_parent import TaskOutcome
from core.domain.values.context.data_types import (
    AncestorData,
    AncestorEntry,
    AncestryChain,
    ArtifactEntry,
    ChildOutcomeEntry,
    ChildOutcomes,
    CoworkerKnowledge,
    CoworkerKnowledgeEntry,
    CustomContext,
    DecisionEntry,
    ParentSummary,
    SharedArtifacts,
    SharedDecisions,
    SiblingEntry,
    SiblingResults,
    SupervisorExpectations,
)
from core.domain.values.context.limits import HierarchyLimits
from core.domain.values.context.parent_to_child import (
    AncestorSummary,
    SpawnPayload,
    build_spawn_payload,
)
from core.domain.values.context.sibling_to_sibling import (
    SharedDecision,
    SiblingStatus,
    SiblingView,
)
from core.domain.values.context.source_context import SourceContext

__all__ = [
    # Base Protocol
    "ContextData",
    # Composable Context Types (new)
    "ParentSummary",
    "AncestorData",
    "AncestorEntry",
    "AncestryChain",
    "SiblingEntry",
    "SiblingResults",
    "DecisionEntry",
    "SharedDecisions",
    "ArtifactEntry",
    "SharedArtifacts",
    "ChildOutcomeEntry",
    "ChildOutcomes",
    "CustomContext",
    "SupervisorExpectations",
    # Coworker Knowledge (Design Choice 5)
    "CoworkerKnowledge",
    "CoworkerKnowledgeEntry",
    # Parent → Child (legacy, still used for structural data)
    "AncestorSummary",
    "SpawnPayload",
    "build_spawn_payload",
    # Child → Parent
    "TaskOutcome",
    # Sibling ↔ Sibling (legacy, consider migrating to SiblingResults)
    "SharedDecision",
    "SiblingStatus",
    "SiblingView",
    # Limits
    "HierarchyLimits",
    # Source Context (Design Choice 6)
    "SourceContext",
]
