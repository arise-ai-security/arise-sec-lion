"""SEC-bench decomposition contract validator (constraints + deterministic fallback).

Enforces legal roles, one-role-per-leaf, same-phase, dependency closure, and
invalid-decision fail-safe. Does NOT select specialists from failure keywords —
that is the Manager's job on re-decomposition.
"""

from __future__ import annotations

import re
from typing import Literal

from core.ports.decomposition_validator_port import (
    DecompositionVerdict,
    DecompositionViolation,
    SuggestedSubtask,
    ValidatedSubtask,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.roles import (
    ADAPTIVE_SPECIALISTS,
    COMPACT_ROLES,
    PHASE_ROLES,
    role_by_name_ci,
)
from plugins.security.route_catalog import (
    dependency_closed_roles,
    phase_from_controller_task,
    role_instruction,
)


_BRACKET = re.compile(r"\[([^\]]+)\]")
_PHASE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "Exploiter": ("Builder",),
    "Fixer": ("Exploiter",),
    "Reporter": ("Builder", "Exploiter", "Fixer"),
}
_DIRECT_ROLE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "Repro-Creator": ("Build-Verifier",),
    "Root-Cause-Analyst": ("Exploit-Validator",),
    "Reporter": ("Build-Verifier", "Exploit-Validator", "Patch-Validator"),
}


def _phase_of(parent_task_description: str) -> str | None:
    return phase_from_controller_task(parent_task_description or "")


def _leaf_for(role_name: str) -> SuggestedSubtask:
    return SuggestedSubtask(
        description=role_instruction(role_name),
        estimated_complexity="simple",
    )


class SecBenchDecompositionValidator:
    """Validate a SEC-bench phase decomposition against the role catalog."""

    def __init__(
        self,
        policy_version: str = "unversioned",
    ) -> None:
        self._policy_version = policy_version

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
    ) -> DecompositionVerdict:
        _ = previously_selected_roles  # available for future multi-attempt tracking
        if not isinstance(domain_context, CVEInstance):
            return DecompositionVerdict(ok=True)
        phase = _phase_of(parent_task_description)
        if phase is None:
            return DecompositionVerdict(ok=True)

        if redecomposition_count > 0:
            return self._classify_adaptive_redecomposition(
                phase=phase,
                subtask_descriptions=subtask_descriptions,
                failed_role_labels=failed_role_labels,
                completed_role_labels=completed_role_labels,
                redecomposition_count=redecomposition_count,
                redecomposition_limit=redecomposition_limit,
            )

        return self._classify_standard(
            phase=phase,
            subtask_descriptions=subtask_descriptions,
        )

    def _classify_standard(
        self,
        *,
        phase: str,
        subtask_descriptions: tuple[str, ...],
    ) -> DecompositionVerdict:
        required_names = set(dependency_closed_roles(phase, COMPACT_ROLES[phase]))
        required = tuple(
            role for role in PHASE_ROLES.get(phase, ()) if role.name in required_names
        )
        if not required:
            return DecompositionVerdict(ok=True)

        violations: list[DecompositionViolation] = []
        removals: list[int] = []
        present: set[str] = set()
        merged_roles: list[str] = []
        validated: list[ValidatedSubtask] = []

        for index, description in enumerate(subtask_descriptions):
            roles = self._catalog_roles_in(description)
            if len(roles) > 1:
                removals.append(index)
                merged_roles.extend(
                    role_name
                    for role_name in roles
                    if self._role_phase(role_name) == phase and role_name in required_names
                )
                violations.append(
                    DecompositionViolation(
                        kind="merged_leaf",
                        detail=f"leaf {index} merges multiple catalog roles: {', '.join(roles)}",
                        subtask_index=index,
                    )
                )
            elif len(roles) == 1:
                role_name = roles[0]
                if self._role_phase(role_name) == phase and role_name in required_names:
                    if role_name in present:
                        removals.append(index)
                        violations.append(
                            DecompositionViolation(
                                kind="duplicate_role",
                                detail=f"leaf {index} duplicates {role_name}",
                                subtask_index=index,
                            )
                        )
                    else:
                        present.add(role_name)
                        validated.append(
                            ValidatedSubtask(
                                subtask_index=index,
                                canonical_description=self._canonical_description(
                                    description, role_name
                                ),
                            )
                        )
                else:
                    removals.append(index)
                    detail = (
                        f"leaf {index} uses {role_name} outside {phase}"
                        if self._role_phase(role_name) != phase
                        else f"leaf {index} uses non-compact initial role {role_name}"
                    )
                    violations.append(
                        DecompositionViolation(
                            kind=(
                                "invalid_role"
                                if self._role_phase(role_name) != phase
                                else "disallowed_role"
                            ),
                            detail=detail,
                            subtask_index=index,
                        )
                    )
            else:
                removals.append(index)
                violations.append(
                    DecompositionViolation(
                        kind="invalid_role",
                        detail=f"leaf {index} has no catalog role",
                        subtask_index=index,
                    )
                )

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

        catalog_order = {role.name: i for i, role in enumerate(PHASE_ROLES.get(phase, ()))}
        inject.sort(key=lambda name: catalog_order.get(name, len(catalog_order)))

        return DecompositionVerdict(
            ok=not violations,
            violations=tuple(violations),
            additions=tuple(_leaf_for(name) for name in inject),
            removals=tuple(removals),
            validated_subtasks=tuple(validated),
        )

    def _classify_adaptive_redecomposition(
        self,
        *,
        phase: str,
        subtask_descriptions: tuple[str, ...],
        failed_role_labels: tuple[str, ...],
        completed_role_labels: tuple[str, ...],
        redecomposition_count: int,
        redecomposition_limit: int,
    ) -> DecompositionVerdict:
        """Validate Manager-authored escalation; expand when no legal role remains."""
        completed = self._canonical_role_names(completed_role_labels)
        failed = self._canonical_role_names(failed_role_labels)
        allowed = self._allowed_escalation_roles(phase, completed=completed, failed=failed)
        allowed_set = set(allowed)

        violations: list[DecompositionViolation] = []
        removals: list[int] = []
        # Roles the Manager intended (including splits from merged leaves).
        manager_roles: list[str] = []
        validated: list[ValidatedSubtask] = []

        for index, description in enumerate(subtask_descriptions):
            roles = self._catalog_roles_in(description)
            if len(roles) > 1:
                removals.append(index)
                violations.append(
                    DecompositionViolation(
                        kind="merged_leaf",
                        detail=f"leaf {index} merges multiple catalog roles: {', '.join(roles)}",
                        subtask_index=index,
                    )
                )
                for role_name in roles:
                    if role_name in allowed_set and role_name not in manager_roles:
                        manager_roles.append(role_name)
                continue

            if not roles:
                removals.append(index)
                violations.append(
                    DecompositionViolation(
                        kind="invalid_role",
                        detail=f"leaf {index} has no legal {phase} catalog role",
                        subtask_index=index,
                    )
                )
                continue

            role_name = roles[0]
            if self._role_phase(role_name) != phase:
                removals.append(index)
                violations.append(
                    DecompositionViolation(
                        kind="invalid_role",
                        detail=f"leaf {index} uses {role_name} outside {phase}",
                        subtask_index=index,
                    )
                )
                continue

            if role_name not in allowed_set:
                removals.append(index)
                violations.append(
                    DecompositionViolation(
                        kind="disallowed_role",
                        detail=f"role not in adaptive catalog for {phase}: {role_name}",
                        subtask_index=index,
                    )
                )
                continue

            if role_name in manager_roles:
                removals.append(index)
                violations.append(
                    DecompositionViolation(
                        kind="duplicate_role",
                        detail=f"leaf {index} duplicates {role_name}",
                        subtask_index=index,
                    )
                )
                continue

            manager_roles.append(role_name)
            validated.append(
                ValidatedSubtask(
                    subtask_index=index,
                    canonical_description=self._canonical_description(
                        description, role_name
                    ),
                )
            )

        # Close hard dependencies. Completed producers are satisfied unless the
        # Manager explicitly selected their owner for evidence-driven correction.
        closed = [
            name
            for name in dependency_closed_roles(phase, tuple(manager_roles))
            if name in manager_roles or name not in completed
        ]
        injected_deps = [name for name in closed if name not in manager_roles]
        if injected_deps:
            violations.append(
                DecompositionViolation(
                    kind="missing_hard_dependency",
                    detail=f"injected hard producers: {', '.join(injected_deps)}",
                )
            )

        final_roles = closed
        route: Literal["expanded", "escalated"]
        triggers: tuple[str, ...]

        if not final_roles:
            # Fail-safe: full remaining same-phase escalation set.
            final_roles = [name for name in allowed if name not in completed]
            route = "expanded"
            triggers = ("manager_decision_invalid", "fallback_expanded")
            violations.append(
                DecompositionViolation(
                    kind="empty_after_correction",
                    detail="no valid role remained; expanded to remaining same-phase set",
                )
            )
            removals = list(range(len(subtask_descriptions)))
            additions = tuple(_leaf_for(name) for name in final_roles)
        else:
            route = "escalated"
            trigger_list = ["phase_gate_failed", "manager_llm"]
            if removals:
                trigger_list.append("host_structural_repair")
            if injected_deps:
                trigger_list.append("host_dependency_closure")
            triggers = tuple(trigger_list)
            present_after = {
                roles[0]
                for i, description in enumerate(subtask_descriptions)
                if i not in removals
                for roles in (self._catalog_roles_in(description),)
                if len(roles) == 1
                and self._role_phase(roles[0]) == phase
            }
            inject_names = [n for n in final_roles if n not in present_after]
            catalog_order = {
                role.name: i for i, role in enumerate(PHASE_ROLES.get(phase, ()))
            }
            inject_names.sort(key=lambda name: catalog_order.get(name, len(catalog_order)))
            additions = tuple(_leaf_for(name) for name in inject_names)

        if not final_roles:
            return DecompositionVerdict(
                ok=False,
                violations=tuple(violations)
                + (
                    DecompositionViolation(
                        kind="unsatisfiable_dependency",
                        detail=f"no runnable {phase} roles remain after completed filter",
                    ),
                ),
                route="expanded",
                triggers=("manager_decision_invalid", "fallback_expanded"),
                policy_version=self._policy_version,
                phase=phase,
                remaining_budget=max(0, redecomposition_limit - redecomposition_count),
                validated_subtasks=tuple(validated),
            )

        ok = not violations and not removals and not additions
        return DecompositionVerdict(
            ok=ok,
            violations=tuple(violations),
            additions=additions,
            removals=tuple(sorted(set(removals))),
            route=route,
            triggers=triggers,
            policy_version=self._policy_version,
            phase=phase,
            remaining_budget=max(0, redecomposition_limit - redecomposition_count),
            validated_subtasks=tuple(validated),
        )

    def _allowed_escalation_roles(
        self,
        phase: str,
        *,
        completed: set[str],
        failed: set[str],
    ) -> tuple[str, ...]:
        """Return every same-phase role a Manager may explicitly select."""
        compact = COMPACT_ROLES.get(phase, ())
        specialists = ADAPTIVE_SPECIALISTS.get(phase, ())
        selected = dependency_closed_roles(phase, (*compact, *specialists))
        extra_failed = tuple(
            role.name
            for name in failed
            if (role := role_by_name_ci(name)) is not None
            and role.name not in selected
            and role.phase == phase
        )
        if extra_failed:
            selected = dependency_closed_roles(phase, (*selected, *extra_failed))
        return selected

    @staticmethod
    def _catalog_roles_in(description: str) -> list[str]:
        out: list[str] = []
        for match in _BRACKET.finditer(description or ""):
            role = role_by_name_ci(match.group(1).strip())
            if role is not None and role.name not in out:
                out.append(role.name)
        return out

    @staticmethod
    def _role_phase(role_name: str) -> str | None:
        role = role_by_name_ci(role_name)
        return role.phase if role is not None else None

    @staticmethod
    def _canonical_role_names(role_labels: tuple[str, ...]) -> set[str]:
        return {
            role.name
            for label in role_labels
            if (role := role_by_name_ci(label)) is not None
        }

    @staticmethod
    def _canonical_description(description: str, role_name: str) -> str:
        stripped = description.strip()
        for match in _BRACKET.finditer(stripped):
            role = role_by_name_ci(match.group(1).strip())
            if role is None or role.name != role_name:
                continue
            instruction = " ".join(
                f"{stripped[:match.start()]} {stripped[match.end():]}".split()
            )
            return f"[{role_name}] {instruction}".strip()
        return f"[{role_name}]"

    def hard_dependencies(self, role_label: str) -> tuple[str, ...]:
        role = role_by_name_ci(role_label)
        phase_dependencies = _PHASE_DEPENDENCIES.get(role_label, ())
        if role is None:
            return phase_dependencies
        return tuple(
            dict.fromkeys(
                (
                    *phase_dependencies,
                    *role.depends_on,
                    *_DIRECT_ROLE_DEPENDENCIES.get(role.name, ()),
                )
            )
        )
