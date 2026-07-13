"""SEC-bench route-catalog helpers shared by policy and validation."""

from __future__ import annotations

from plugins.security.roles import PHASE_ORDER, PHASE_ROLES, role_by_name_ci


def phase_controller_instruction(phase: str) -> str:
    """Build the trusted task that uniquely identifies one phase controller."""
    return f"[{phase}] Execute the {phase} phase outcome gate."


def phase_from_controller_task(task_description: str) -> str | None:
    """Recognize only Host-issued controller tasks, not same-named leaf roles."""
    normalized = task_description.strip()
    for phase in PHASE_ORDER:
        if normalized == phase_controller_instruction(phase):
            return phase
    return None


def dependency_closed_roles(phase: str, role_names: tuple[str, ...]) -> tuple[str, ...]:
    """Return selected roles plus transitive hard producers in catalog order."""
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
    return tuple(role.name for role in PHASE_ROLES.get(phase, ()) if role.name in selected)


def role_instruction(role_name: str) -> str:
    """Build the trusted initial task instruction for one catalog role."""
    role = role_by_name_ci(role_name)
    if role is None:
        return f"[{role_name}]"
    objective = " ".join(part for part in (role.summary, role.insight) if part)
    return f"[{role.name}] {objective}".strip()
