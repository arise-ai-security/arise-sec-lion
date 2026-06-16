"""SEC-bench implementation of the decomposition role-contract validator.

Enforces, for a phase manager's decomposition, that every REQUIRED catalog role of the
parent phase appears as its own single-role leaf. Missing roles are suggested for
injection; a leaf that merges more than one catalog role of the phase is flagged for
removal and split into one leaf per role. The role contract is read from
``plugins.security.roles`` so it stays the single source of truth.
"""

from __future__ import annotations

import re

from core.ports.decomposition_validator_port import (
    DecompositionVerdict,
    DecompositionViolation,
    SuggestedSubtask,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.roles import PHASE_ROLES, role_by_name_ci


_BRACKET = re.compile(r"\[([^\]]+)\]")


class SecBenchDecompositionValidator:
    """Validate a SEC-bench phase decomposition against the role catalog."""

    def classify(
        self,
        *,
        parent_task_description: str,
        subtask_descriptions: tuple[str, ...],
        domain_context: object | None,
    ) -> DecompositionVerdict:
        # Only CVE phase decompositions carry a role contract; everything else is a no-op
        # so the gate never touches non-SEC-bench or boss-level (no phase bracket) runs.
        if not isinstance(domain_context, CVEInstance):
            return DecompositionVerdict(ok=True)
        phase = self._phase_of(parent_task_description)
        if phase is None:
            return DecompositionVerdict(ok=True)
        required = tuple(role for role in PHASE_ROLES.get(phase, ()) if role.required)
        if not required:
            return DecompositionVerdict(ok=True)

        violations: list[DecompositionViolation] = []
        removals: list[int] = []
        present: set[str] = set()
        merged_roles: list[str] = []

        for index, description in enumerate(subtask_descriptions):
            roles = self._catalog_roles_in(description, phase)
            if len(roles) > 1:
                # Merged leaf: the router only honours the leading bracket, so the
                # trailing role's deliverable is silently dropped. Drop + split it.
                removals.append(index)
                merged_roles.extend(roles)
                violations.append(
                    DecompositionViolation(
                        kind="merged_leaf",
                        detail=f"leaf {index} merges multiple {phase} roles: {', '.join(roles)}",
                        subtask_index=index,
                    )
                )
            elif len(roles) == 1:
                present.add(roles[0])

        # Roles to inject: merged-leaf roles not also present as a clean leaf, plus any
        # missing required role. Each is queued at most once.
        inject: list[str] = []
        for role_name in merged_roles:
            if role_name not in present and role_name not in inject:
                inject.append(role_name)
        for role in required:
            if role.name not in present and role.name not in inject:
                violations.append(
                    DecompositionViolation(
                        kind="missing_required_role",
                        detail=f"required {phase} role not spawned: {role.name}",
                    )
                )
                inject.append(role.name)

        # Emit injections in catalog (execution) order so a producer leaf precedes its
        # consumer when appended (depends_on is also enforced as DAG edges by the scheduler).
        catalog_order = {role.name: i for i, role in enumerate(PHASE_ROLES.get(phase, ()))}
        inject.sort(key=lambda name: catalog_order.get(name, len(catalog_order)))

        return DecompositionVerdict(
            ok=not violations,
            violations=tuple(violations),
            additions=tuple(self._leaf_for(name) for name in inject),
            removals=tuple(removals),
        )

    @staticmethod
    def _phase_of(parent_task_description: str) -> str | None:
        """Resolve the parent's phase from its LEADING bracket, or None.

        Only a *phase* bracket ([Builder]/[Exploiter]/[Fixer]/[Reporter]) owns a leaf-role
        contract. A parent bracketed with a leaf role (a worker that itself decomposed)
        does NOT own the phase's full role set, so it resolves to None (no enforcement).
        Anchored at the start so an incidental phase name mid-text does not trigger a contract.
        """
        match = _BRACKET.match((parent_task_description or "").lstrip())
        if match is None:
            return None
        label = match.group(1).strip()
        return label if label in PHASE_ROLES else None

    @staticmethod
    def _catalog_roles_in(description: str, phase: str) -> list[str]:
        """Catalog roles of ``phase`` referenced by brackets in this leaf, in order."""
        out: list[str] = []
        for match in _BRACKET.finditer(description or ""):
            role = role_by_name_ci(match.group(1).strip())
            if role is not None and role.phase == phase and role.name not in out:
                out.append(role.name)
        return out

    @staticmethod
    def _leaf_for(role_name: str) -> SuggestedSubtask:
        role = role_by_name_ci(role_name)
        if role is None:
            return SuggestedSubtask(description=f"[{role_name}]")
        # Carry the role's full objective (summary + empirical insight) and its hard
        # catalog deps so the injected leaf is as rich as a manager-authored one and is
        # scheduled after its producer.
        objective = " ".join(part for part in (role.summary, role.insight) if part)
        return SuggestedSubtask(
            description=f"[{role.name}] {objective}".strip(),
            estimated_complexity="simple",
        )

    @staticmethod
    def hard_dependencies(role_label: str) -> tuple[str, ...]:
        role = role_by_name_ci(role_label)
        return role.depends_on if role is not None else ()
