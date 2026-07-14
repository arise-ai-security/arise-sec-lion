"""Port for Manager-authored decomposition validation.

``DecompositionValidator`` classifies decompositions against a domain role contract;
core owns the repair action.

Core stays domain-ignorant: it passes child task descriptions plus opaque
``domain_context`` and applies additions/removals mechanically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class SuggestedSubtask:
    """A leaf the validator asks core to inject to satisfy the role contract."""

    description: str
    estimated_complexity: Literal["simple", "complex", "unknown"] = "simple"
    task_type: str = "general"


@dataclass(frozen=True)
class DecompositionViolation:
    """One classified contract breach (for the failure reason / observability)."""

    kind: str  # "missing_required_role" | "merged_leaf" | "unsatisfiable_dependency" | ...
    detail: str
    subtask_index: int = -1  # offending child index, or -1 when not leaf-specific


@dataclass(frozen=True)
class ValidatedSubtask:
    """Canonical description for one accepted, atomic catalog leaf."""

    subtask_index: int
    canonical_description: str


@dataclass(frozen=True)
class DecompositionVerdict:
    """Classification of a decomposition plus the suggested repair delta.

    ``additions`` are leaves to inject; ``removals`` are indices of existing leaves to
    drop (merged/invalid). Core applies removals then additions and re-checks limits.

    When the domain attaches route provenance (``route`` is set), core records a
    ``PhaseRouteSelected`` event from the final corrected role list.
    """

    ok: bool
    violations: tuple[DecompositionViolation, ...] = ()
    additions: tuple[SuggestedSubtask, ...] = ()
    removals: tuple[int, ...] = ()
    # Adaptive re-decomposition provenance (None → no PhaseRouteSelected from verdict).
    route: Literal["compact", "expanded", "escalated"] | None = None
    triggers: tuple[str, ...] = ()
    policy_version: str = ""
    phase: str = ""
    remaining_budget: int = 0
    evidence_references: tuple[str, ...] = ()
    validated_subtasks: tuple[ValidatedSubtask, ...] = ()


@dataclass(frozen=True)
class DecompositionRoute:
    """Provenance for a domain-validated decomposition route."""

    policy_version: str
    phase: str
    route: Literal["compact", "expanded", "escalated"]
    triggers: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    remaining_budget: int = 0


class DecompositionValidator(Protocol):
    """Validate a parent's proposed child decomposition against a domain role contract."""

    def classify(
        self,
        *,
        parent_task_description: str,
        subtask_descriptions: tuple[str, ...],
        domain_context: object | None,
        redecomposition_count: int = 0,
        previously_selected_roles: tuple[str, ...] = (),
        failed_role_labels: tuple[str, ...] = (),
        completed_role_labels: tuple[str, ...] = (),
        redecomposition_limit: int = 0,
    ) -> DecompositionVerdict: ...

    def hard_dependencies(self, role_label: str) -> tuple[str, ...]:
        """The catalog HARD-producer role labels a given role must run after.

        Core resolves these to sibling indices to enforce the authoritative DAG on EVERY
        catalog-role leaf (manager-authored and injected alike). Empty for an unknown
        role or one with no hard producer.
        """
        ...
