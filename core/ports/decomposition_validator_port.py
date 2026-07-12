"""Port for validating a parent's proposed child decomposition against a role contract.

A domain plugin declares a *role contract*: which roles a phase must spawn and how. An
LLM manager decomposes free-form, so it can omit a required role or merge several roles
into one leaf. This port lets the domain CLASSIFY such violations; the core orchestrator
OWNS the enforcement ACTION (inject the missing leaves, drop/split the merged ones, or
fail before spawning when hierarchy limits make repair impossible).

Core stays domain-ignorant: it passes child task descriptions (strings) plus the opaque
``domain_context`` and applies the returned additions/removals mechanically.
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

    kind: str  # "missing_required_role" | "merged_leaf" | "unsatisfiable_dependency"
    detail: str
    subtask_index: int = -1  # offending child index, or -1 when not leaf-specific


@dataclass(frozen=True)
class DecompositionVerdict:
    """Classification of a decomposition plus the suggested repair delta.

    ``additions`` are leaves to inject; ``removals`` are indices of existing leaves to
    drop (merged/invalid). Core applies removals then additions and re-checks limits.
    """

    ok: bool
    violations: tuple[DecompositionViolation, ...] = ()
    additions: tuple[SuggestedSubtask, ...] = ()
    removals: tuple[int, ...] = ()


@dataclass(frozen=True)
class FixedDecomposition:
    """Host-selected deterministic decomposition and its route provenance."""

    subtasks: tuple[SuggestedSubtask, ...]
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
    ) -> DecompositionVerdict: ...

    def hard_dependencies(self, role_label: str) -> tuple[str, ...]:
        """The catalog HARD-producer role labels a given role must run after.

        Core resolves these to sibling indices to enforce the authoritative DAG on EVERY
        catalog-role leaf (manager-authored and injected alike). Empty for an unknown
        role or one with no hard producer.
        """
        ...

    def fixed_decomposition(
        self,
        *,
        parent_task_description: str,
        domain_context: object | None,
        redecomposition_count: int,
    ) -> FixedDecomposition | None:
        """Return a host-defined decomposition, or None to use LLM decomposition."""
        ...
