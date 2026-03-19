"""Context value objects for inter-agent communication.

Canonical types now live in ``core.domain.values.node_message`` and
``core.domain.values.limits``.  This package re-exports them so existing
imports continue to work.
"""

from core.domain.values.limits import HierarchyLimits
from core.domain.values.node_message import (
    Ancestor,
    Briefing,
    Handoff,
    NodeMessage,
    PeerStatus,
    Report,
    SharedDecision,
    build_briefing,
)

__all__ = [
    # Down (parent → child)
    "Ancestor",
    "Briefing",
    "build_briefing",
    # Up (child → parent)
    "Report",
    # Lateral (sibling ↔ sibling)
    "Handoff",
    "PeerStatus",
    "SharedDecision",
    # Union
    "NodeMessage",
    # Limits
    "HierarchyLimits",
]
