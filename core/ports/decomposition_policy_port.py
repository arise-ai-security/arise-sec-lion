"""Port for deterministic initial decomposition policies.

The host may own a domain-specific initial control plan while leaving failure
analysis and adaptive role selection to an LLM Manager. Core applies the plan
mechanically and remains domain-ignorant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class InitialSubtask:
    """One child in a host-selected initial decomposition."""

    description: str
    estimated_complexity: Literal["simple", "complex", "unknown"] = "simple"
    task_type: str = "general"
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class InitialDecomposition:
    """A deterministic initial decomposition and its route provenance."""

    subtasks: tuple[InitialSubtask, ...]
    policy_version: str
    phase: str
    route: Literal["compact", "expanded", "escalated"] = "compact"
    triggers: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()


class InitialDecompositionPolicy(Protocol):
    """Select a host-owned initial plan without interpreting failure evidence."""

    def select_initial(
        self,
        *,
        parent_task_description: str,
        domain_context: object | None,
        is_root: bool,
        redecomposition_count: int,
    ) -> InitialDecomposition | None: ...
