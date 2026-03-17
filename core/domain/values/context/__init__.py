"""Context value objects for inter-agent communication.

Organized by data flow direction:
- parent_to_child: Parent → Child (spawn-time payload)
- child_to_parent: Child → Parent (task results)
- sibling_to_sibling: Child ↔ Child (worker coordination)
- limits: Execution constraints (hierarchy limits)
"""

from core.domain.values.context.child_to_parent import TaskOutcome
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

__all__ = [
    # Parent → Child
    "AncestorSummary",
    "SpawnPayload",
    "build_spawn_payload",
    # Child → Parent
    "TaskOutcome",
    # Sibling ↔ Sibling
    "SharedDecision",
    "SiblingStatus",
    "SiblingView",
    # Limits
    "HierarchyLimits",
]
