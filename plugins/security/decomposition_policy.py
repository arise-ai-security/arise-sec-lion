"""Deterministic SEC-bench initial decomposition policy."""

from __future__ import annotations

from core.ports.decomposition_policy_port import InitialDecomposition, InitialSubtask
from plugins.security.cve_instance import CVEInstance
from plugins.security.roles import COMPACT_ROLES, PHASE_ORDER, role_by_name_ci
from plugins.security.route_catalog import (
    dependency_closed_roles,
    phase_controller_instruction,
    phase_from_controller_task,
    role_instruction,
)


_PHASE_DEPENDENCIES: tuple[tuple[int, ...], ...] = ((), (0,), (1,), (0, 1, 2))


class SecBenchInitialDecompositionPolicy:
    """Create the canonical phase skeleton and compact role plans."""

    def __init__(self, policy_version: str) -> None:
        self._policy_version = policy_version

    def select_initial(
        self,
        *,
        parent_task_description: str,
        domain_context: object | None,
        is_root: bool,
        redecomposition_count: int,
    ) -> InitialDecomposition | None:
        if not isinstance(domain_context, CVEInstance):
            return None
        if is_root:
            if redecomposition_count > 0:
                return None
            return InitialDecomposition(
                subtasks=tuple(
                    InitialSubtask(
                        description=phase_controller_instruction(phase),
                        estimated_complexity="complex",
                        depends_on=_PHASE_DEPENDENCIES[index],
                    )
                    for index, phase in enumerate(PHASE_ORDER)
                ),
                policy_version=self._policy_version,
                phase="skeleton",
                triggers=("run_started",),
            )

        if redecomposition_count > 0:
            return None
        phase = self._phase_of(parent_task_description)
        if phase is None:
            return None
        role_names = dependency_closed_roles(phase, COMPACT_ROLES[phase])
        role_indices = {name: index for index, name in enumerate(role_names)}
        subtasks = []
        for role_name in role_names:
            role = role_by_name_ci(role_name)
            dependencies = () if role is None else role.depends_on
            subtasks.append(
                InitialSubtask(
                    description=role_instruction(role_name),
                    estimated_complexity="simple",
                    depends_on=tuple(
                        role_indices[dependency]
                        for dependency in dependencies
                        if dependency in role_indices
                    ),
                )
            )
        return InitialDecomposition(
            subtasks=tuple(subtasks),
            policy_version=self._policy_version,
            phase=phase,
            triggers=("phase_started",),
        )

    @staticmethod
    def _phase_of(parent_task_description: str) -> str | None:
        return phase_from_controller_task(parent_task_description)
