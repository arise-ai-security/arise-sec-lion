"""Decomposition-contract enforcement, catalog DAG, and sibling dedup.

Core owns the enforcement ACTION over a domain validator's verdict: drop merged
leaves, inject missing required roles (inheriting sibling worker config), resolve
the authoritative catalog dependency DAG onto every leaf, and dedup subtasks whose
role prefix already exists among siblings / across the tree. Classification itself
is a domain concern (the injected ``DecompositionValidator``); this service is
domain-agnostic. Spawning stays on the orchestrator — this only validates.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import Any

    from core.application.services import ChildAgentFactory
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.subtask import Subtask
    from core.ports.decomposition_validator_port import DecompositionValidator

logger = logging.getLogger(__name__)


class DecompositionContractService:
    """Enforces the domain role contract and catalog DAG on a decomposition."""

    # Role-prefix pattern: "[Some-Role]" → "Some-Role"
    _BRACKET_PREFIX = re.compile(r"^\[([^\]]+)\]")

    def __init__(
        self,
        child_factory: ChildAgentFactory,
        decomposition_validator: DecompositionValidator | None = None,
    ) -> None:
        self._child_factory = child_factory
        self._decomposition_validator = decomposition_validator

    async def enforce_decomposition_contract(
        self,
        agent: AgentSession,
        subtasks: list[Subtask],
        dedup_map: dict[int, int] | None = None,
    ) -> list[Subtask] | None:
        """Apply the domain role contract to a (already-deduped) decomposition before spawning.

        ``dedup_map`` (original→post-dedup index) lets authored non-catalog ``depends_on``
        be remapped through an earlier dedup shift so they are not stale.

        The domain validator classifies contract breaches (missing required roles,
        merged-role leaves); core owns the action: drop the flagged leaves, inject the
        suggested ones (inheriting sibling worker config), and re-check hierarchy limits.
        Returns the corrected subtasks, or None if the repair cannot fit the limits (the
        agent is failed before any child is spawned). A no-op when no validator is wired
        or the verdict is clean.
        """
        if self._decomposition_validator is None:
            return subtasks
        domain_context = agent.hierarchy_limits.domain_context if agent.hierarchy_limits else None
        verdict = self._decomposition_validator.classify(
            parent_task_description=agent.task_description,
            subtask_descriptions=tuple(st.description for st in subtasks),
            domain_context=domain_context,
        )
        if verdict.ok:
            # Even a role-complete decomposition can carry missing/wrong depends_on;
            # enforce the authoritative catalog DAG on the manager's own leaves.
            return self.resolve_catalog_dependencies(subtasks, dedup_map)

        from core.domain.values.subtask import Subtask

        removals = set(verdict.removals)
        base_config = dict(subtasks[0].config) if subtasks else {}
        old_to_new: dict[int, int] = {}
        corrected: list[Subtask] = []
        for old_index, subtask in enumerate(subtasks):
            if old_index in removals:
                continue
            old_to_new[old_index] = len(corrected)
            corrected.append(subtask)
        corrected.extend(
            Subtask(
                description=add.description,
                config=dict(base_config),
                estimated_complexity=add.estimated_complexity,
                task_type=add.task_type,
            )
            for add in verdict.additions
        )
        # Enforce the authoritative catalog DAG on EVERY leaf (authored + injected). Remap
        # non-catalog authored deps through the full original→corrected shift: compose the
        # earlier dedup shift (original→deduped) with this removal shift (deduped→corrected).
        if dedup_map is not None:
            index_shift = {
                orig: old_to_new[deduped]
                for orig, deduped in dedup_map.items()
                if deduped in old_to_new
            }
        else:
            index_shift = old_to_new
        corrected = self.resolve_catalog_dependencies(corrected, index_shift)
        logger.warning(
            "Agent %s: decomposition-contract repair %s — dropped %d, injected %d "
            "(%d -> %d children)",
            agent.agent_id,
            [v.kind for v in verdict.violations],
            len(removals),
            len(verdict.additions),
            len(subtasks),
            len(corrected),
        )

        # Core owns the limit action: a repair that would exceed the caps is a hard
        # failure surfaced before spawning, not a silently-incomplete decomposition.
        failure = await self.check_limit_violations(agent, corrected)
        if failure:
            agent.fail_with_reason(
                f"decomposition contract unsatisfiable within hierarchy limits: {failure}"
            )
            return None
        return corrected

    def resolve_catalog_dependencies(
        self, subtasks: list[Subtask], old_to_new: dict[int, int] | None = None
    ) -> list[Subtask]:
        """Set each leaf's depends_on to its catalog HARD producers (resolved to sibling
        indices), enforcing the authoritative DAG on every catalog-role leaf — authored OR
        injected — regardless of what the manager emitted. A leaf whose role has no catalog
        hard dep keeps its own depends_on, remapped via ``old_to_new`` when removals shifted
        sibling indices. Only producers that exist in the set are referenced.
        """
        validator = self._decomposition_validator
        if validator is None:
            return subtasks
        role_to_index: dict[str, int] = {}
        for index, subtask in enumerate(subtasks):
            prefix = self._extract_role_prefix(subtask.description)
            if prefix:
                role_to_index.setdefault(prefix.lower(), index)
        resolved: list[Subtask] = []
        for subtask in subtasks:
            prefix = self._extract_role_prefix(subtask.description)
            catalog_deps = validator.hard_dependencies(prefix) if prefix else ()
            if catalog_deps:
                new_deps = [
                    role_to_index[dep.lower()]
                    for dep in catalog_deps
                    if dep.lower() in role_to_index
                ]
            elif old_to_new is not None and subtask.depends_on:
                new_deps = [old_to_new[d] for d in subtask.depends_on if d in old_to_new]
            else:
                new_deps = list(subtask.depends_on)
            if new_deps != list(subtask.depends_on):
                resolved.append(subtask.model_copy(update={"depends_on": new_deps}))
            else:
                resolved.append(subtask)
        return resolved

    async def check_limit_violations(self, agent: AgentSession, subtasks: list) -> str | None:
        """Check hard limits. Returns failure reason or None.

        Async to compose with the atomic reservation path in
        ``_spawn_children``; the cap check itself is sync but the
        cascade keeps the call chain awaitable end-to-end (D.2).
        """
        limits = agent.hierarchy_limits
        violations: list[dict[str, Any]] = []

        # Hard limit: children per node
        if (
            limits is not None
            and limits.is_children_limited()
            and len(subtasks) > limits.max_children_per_node
        ):
            violations.append(
                {
                    "limit_type": "children",
                    "limit_value": limits.max_children_per_node,
                    "attempted_value": len(subtasks),
                    "action_taken": "agent_failed",
                }
            )

        # Hard limit: total agents
        max_total = self._child_factory.max_total_agents
        if max_total > 0:
            current_total = self._child_factory.total_created
            remaining = max_total - current_total
            if len(subtasks) > remaining:
                violations.append(
                    {
                        "limit_type": "total_agents",
                        "limit_value": max_total,
                        "attempted_value": current_total + len(subtasks),
                        "action_taken": "agent_failed",
                    }
                )

        if violations:
            for v in violations:
                agent.emit_limit_enforced(**v)
            types = [v["limit_type"] for v in violations]
            return f"LLM violated limits: {types}"

        return None

    @classmethod
    def _extract_role_prefix(cls, description: str) -> str | None:
        """Extract [Bracketed-Role-Name] from a task description."""
        m = cls._BRACKET_PREFIX.match(description.strip())
        return m.group(1) if m else None

    def dedup_subtasks_against_siblings(
        self,
        agent: AgentSession,
        subtasks: list[Subtask],
    ) -> list[Subtask]:
        """Remove subtasks whose role names duplicate the agent's own siblings.

        When a manager re-decomposes into the exact same roles as its
        siblings, all those workers are redundant — the siblings already
        cover them.  This is a common Qwen failure mode.

        Returns:
            Filtered subtask list (may be empty).
        """
        if agent.parent_id is None:
            return subtasks  # Boss-level decomposition — no siblings to clash with

        # Collect uncle role names (siblings of the current agent)
        uncle_roles = self._get_sibling_role_prefixes(agent)

        # Collect tree-wide role names to catch cross-subtree duplication
        tree_roles = self._get_tree_role_prefixes(agent)

        if not uncle_roles and not tree_roles:
            return subtasks  # No siblings or role info — skip check

        # Also include the agent's own role as off-limits for children
        own_role = self._extract_role_prefix(agent.task_description or "")
        forbidden = uncle_roles | tree_roles | ({own_role} if own_role else set())

        kept: list[Subtask] = []
        for st in subtasks:
            prefix = self._extract_role_prefix(st.description)
            if prefix and prefix in forbidden:
                logger.info(
                    "Agent %s: stripping duplicate subtask [%s] (already covered by sibling/self)",
                    agent.agent_id,
                    prefix,
                )
                continue
            kept.append(st)

        if kept and len(kept) < len(subtasks):
            logger.info(
                "Agent %s: kept %d/%d subtasks after dedup",
                agent.agent_id,
                len(kept),
                len(subtasks),
            )

        return kept

    def _get_sibling_role_prefixes(self, agent: AgentSession) -> set[str]:
        """Get [Role-Name] prefixes of the agent's siblings (same parent)."""
        if agent.parent_id is None:
            return set()
        registry = getattr(self._child_factory, "_limits_registry", None)
        if registry is None:
            return set()
        return registry.get_sibling_role_prefixes(agent.agent_id, agent.parent_id)

    def _get_tree_role_prefixes(self, agent: AgentSession) -> set[str]:
        """Get ALL [Role-Name] prefixes used anywhere in the execution tree."""
        registry = getattr(self._child_factory, "_limits_registry", None)
        if registry is None:
            return set()
        return registry.get_tree_role_prefixes(agent.agent_id)
