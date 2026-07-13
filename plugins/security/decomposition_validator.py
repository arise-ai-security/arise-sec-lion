"""SEC-bench adaptive and role-contract decomposition policy.

Adaptive treatments select dependency-closed compact or escalated routes. Legacy phase
decompositions still enforce required single-role leaves. The role-fused treatment emits
four LLM phase workers and four host procedure roles directly under the boss.
"""

from __future__ import annotations

import re
from typing import Literal, cast

from core.ports.decomposition_validator_port import (
    DecompositionVerdict,
    DecompositionViolation,
    FixedDecomposition,
    SuggestedSubtask,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.roles import PHASE_ROLES, role_by_name_ci


_BRACKET = re.compile(r"\[([^\]]+)\]")


class SecBenchDecompositionValidator:
    """Validate a SEC-bench phase decomposition against the role catalog."""

    _PHASES = ("Builder", "Exploiter", "Fixer", "Reporter")
    _PHASE_DEPS = {
        "Exploiter": ("Builder",),
        "Fixer": ("Exploiter",),
        "Reporter": ("Fixer",),
    }
    _COMPACT = {
        "Builder": ("Build-Executor", "Build-Verifier"),
        "Exploiter": ("Repro-Creator", "Exploit-Validator"),
        "Fixer": ("Root-Cause-Analyst", "Patch-Applier", "Patch-Validator"),
        "Reporter": ("Reporter",),
    }
    _ROLE_FUSED_ROOT = (
        "Builder",
        "Build-Verifier",
        "Exploiter",
        "Exploit-Validator",
        "Fixer",
        "Patch-Applier",
        "Patch-Validator",
        "Reporter",
    )
    _ROLE_FUSED_DEPS = {
        "Build-Verifier": ("Builder",),
        "Exploiter": ("Build-Verifier",),
        "Exploit-Validator": ("Exploiter",),
        "Fixer": ("Exploit-Validator",),
        "Patch-Applier": ("Fixer",),
        "Patch-Validator": ("Patch-Applier",),
        "Reporter": ("Patch-Validator",),
    }
    _ESCALATED = {
        "Builder": ("Build-Setup", "Build-Executor", "Build-Verifier"),
        "Exploiter": (
            "PoC-Researcher",
            "PoC-Tester",
            "Repro-Creator",
            "Exploit-Validator",
        ),
        "Fixer": (
            "Root-Cause-Analyst",
            "Candidate-Reviewer",
            "Regression-Tester",
            "Patch-Applier",
            "Patch-Validator",
        ),
        "Reporter": ("Reporter",),
    }
    # Trigger-conditioned expansion: which same-phase specialists a specific
    # failure signal calls for, added on top of the compact roles.
    _EXPANSION: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = {
        "Builder": (
            (("build", "compile", "setup", "dependenc", "binary"),
             ("Build-Setup", "Build-Verifier")),
        ),
        "Exploiter": (
            (("sanitizer", "poc", "trigger", "crash", "asan"),
             ("PoC-Researcher", "PoC-Tester")),
            (("reproduc", "conflict", "data flow", "dataflow", "call graph"),
             ("Data-Flow-Analyst", "Forward-Instrumentator")),
        ),
        "Fixer": (
            (("candidate", "fix layer", "multiple", "over-broad", "overbroad"),
             ("Candidate-Reviewer",)),
            (("regression", "patch validation", "rebuild", "still crash", "revert"),
             ("Regression-Tester",)),
        ),
    }

    def __init__(
        self, adaptive_execution: bool = False, policy_version: str = "b4-adaptive-v1"
    ) -> None:
        self._adaptive_execution = adaptive_execution
        # The treatment/policy version is orchestration config, supplied by the
        # composition root — not owned here. Stamped onto route provenance so it
        # cannot silently drift from settings.orchestration.treatment_version.
        self._policy_version = policy_version
        self._role_fused = policy_version == "b4-adaptive-rolefused-v1"

    @staticmethod
    def safe_route(
        requested: str | None,
        *,
        decision_complete: bool,
    ) -> Literal["compact", "expanded", "escalated"]:
        """Fail incomplete or invalid route decisions to expanded execution."""
        if not decision_complete or requested not in {"compact", "expanded", "escalated"}:
            return "expanded"
        return cast(Literal["compact", "expanded", "escalated"], requested)

    def _escalated_roles(self, phase: str, failure_signal: str) -> tuple[str, ...]:
        """Compact roles plus the specialists a specific failure signal calls for.

        Preserves the phase's compact roles (their successful artifacts are reused,
        not restarted) and adds only the targeted specialists for the observed
        failure. With no recognizable signal, falls back to the phase's full
        specialist set. Stays within the phase — never injects the whole catalog.
        """
        compact = self._COMPACT.get(phase, ())
        signal = failure_signal.lower()
        matched: list[str] = []
        for keywords, specialists in self._EXPANSION.get(phase, ()):
            if any(keyword in signal for keyword in keywords):
                matched.extend(specialists)
        if not matched:
            return self._ESCALATED.get(phase, compact)
        selected = list(dict.fromkeys((*compact, *matched)))
        order = {role.name: i for i, role in enumerate(PHASE_ROLES.get(phase, ()))}
        selected.sort(key=lambda name: order.get(name, len(order)))
        return tuple(selected)

    @staticmethod
    def _with_hard_dependencies(phase: str, role_names: tuple[str, ...]) -> tuple[str, ...]:
        """Return selected roles plus every transitive hard producer in catalog order."""
        selected = set(role_names)
        changed = True
        while changed:
            changed = False
            for role in PHASE_ROLES.get(phase, ()):
                if role.name not in selected:
                    continue
                for dependency in role.depends_on:
                    if dependency not in selected:
                        selected.add(dependency)
                        changed = True
        return tuple(
            role.name for role in PHASE_ROLES.get(phase, ()) if role.name in selected
        )

    def fixed_decomposition(
        self,
        *,
        parent_task_description: str,
        domain_context: object | None,
        redecomposition_count: int,
        failure_signal: str = "",
    ) -> FixedDecomposition | None:
        if not self._adaptive_execution or not isinstance(domain_context, CVEInstance):
            return None
        phase = self._phase_of(parent_task_description)
        if phase is None:
            if _BRACKET.match((parent_task_description or "").lstrip()) is not None:
                return None
            root_roles = self._ROLE_FUSED_ROOT if self._role_fused else self._PHASES
            subtasks = tuple(
                SuggestedSubtask(
                    description=f"[{name}] Execute the {name} phase outcome gate.",
                    estimated_complexity="simple" if self._role_fused else "complex",
                )
                for name in root_roles
            )
            return FixedDecomposition(
                subtasks=subtasks,
                policy_version=self._policy_version,
                phase="skeleton",
                route="compact",
                triggers=("run_started",),
                remaining_budget=1,
            )
        if self._role_fused:
            return None
        if redecomposition_count == 0:
            role_names = self._with_hard_dependencies(phase, self._COMPACT[phase])
            route_name = self.safe_route("compact", decision_complete=True)
        else:
            role_names = self._with_hard_dependencies(
                phase, self._escalated_roles(phase, failure_signal)
            )
            route_name = self.safe_route("escalated", decision_complete=True)
        return FixedDecomposition(
            subtasks=tuple(self._leaf_for(name) for name in role_names),
            policy_version=self._policy_version,
            phase=phase,
            route=route_name,
            triggers=("upstream_success",) if route_name == "compact" else ("phase_gate_failed",),
            remaining_budget=max(0, 1 - redecomposition_count),
        )

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
        if self._adaptive_execution:
            required_names = set(
                self._with_hard_dependencies(phase, self._COMPACT[phase])
            )
            required = tuple(
                role for role in PHASE_ROLES.get(phase, ()) if role.name in required_names
            )
        else:
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

    def hard_dependencies(self, role_label: str) -> tuple[str, ...]:
        if self._role_fused and role_label in self._ROLE_FUSED_DEPS:
            return self._ROLE_FUSED_DEPS[role_label]
        if self._adaptive_execution and role_label in self._PHASE_DEPS:
            return self._PHASE_DEPS[role_label]
        role = role_by_name_ci(role_label)
        return role.depends_on if role is not None else ()
