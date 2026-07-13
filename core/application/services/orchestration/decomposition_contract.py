"""Decomposition-contract enforcement, catalog DAG, and sibling dedup.

Core owns the enforcement ACTION over a domain validator's verdict: drop merged
leaves, inject missing required roles (inheriting sibling worker config), resolve
the authoritative catalog dependency DAG onto every leaf, and dedup subtasks whose
role prefix is completed or already covered elsewhere in the tree. Classification
itself is a domain concern (the injected ``DecompositionValidator``). Spawning stays
on the orchestrator — this only validates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from typing import Any

    from core.application.services import ChildAgentFactory
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.values.subtask import Subtask
    from core.ports.decomposition_policy_port import (
        InitialDecomposition,
        InitialDecompositionPolicy,
        InitialSubtask,
    )
    from core.ports.decomposition_validator_port import (
        DecompositionRoute,
        DecompositionValidator,
        ValidatedSubtask,
    )

logger = logging.getLogger(__name__)


class DecompositionContractService:
    """Enforces the domain role contract and catalog DAG on a decomposition."""

    # Role-prefix pattern: "[Some-Role]" → "Some-Role"
    _BRACKET_PREFIX = re.compile(r"^\[([^\]]+)\]")

    def __init__(
        self,
        child_factory: ChildAgentFactory,
        decomposition_policy: InitialDecompositionPolicy | None = None,
        decomposition_validator: DecompositionValidator | None = None,
        include_manager_layer: bool = True,
        max_redecompositions: int = 0,
    ) -> None:
        self._child_factory = child_factory
        self._decomposition_policy = decomposition_policy
        self._decomposition_validator = decomposition_validator
        self._include_manager_layer = include_manager_layer
        self._max_redecompositions = max_redecompositions

    def initial_decomposition(
        self, agent: AgentSession
    ) -> tuple[list[Subtask], DecompositionRoute] | None:
        """Build the host-selected initial plan for this parent, when configured."""
        policy = self._decomposition_policy
        if policy is None:
            return None
        domain_context = agent.hierarchy_limits.domain_context if agent.hierarchy_limits else None
        selection = policy.select_initial(
            parent_task_description=agent.task_description,
            domain_context=domain_context,
            is_root=agent.parent_id is None,
            redecomposition_count=agent.redecomposition_count,
        )
        if selection is None:
            return None
        if not self._include_manager_layer and agent.parent_id is None:
            selection = self._flatten_manager_layer(selection, domain_context)

        from core.domain.values.subtask import Subtask
        from core.ports.decomposition_validator_port import DecompositionRoute

        config = agent.config.model_dump()
        subtasks = [
            Subtask(
                description=suggestion.description,
                config=dict(config),
                estimated_complexity=suggestion.estimated_complexity,
                task_type=suggestion.task_type,
                depends_on=list(suggestion.depends_on),
                selection_source="host_policy",
            )
            for suggestion in selection.subtasks
        ]
        route = DecompositionRoute(
            policy_version=selection.policy_version,
            phase=selection.phase,
            route=selection.route,
            triggers=selection.triggers,
            evidence_references=selection.evidence_references,
            remaining_budget=max(0, self._max_redecompositions - agent.redecomposition_count),
        )
        return subtasks, route

    def _flatten_manager_layer(
        self,
        selection: InitialDecomposition,
        domain_context: object | None,
    ) -> InitialDecomposition:
        """Flatten one deterministic grouping layer while preserving its dependency DAG."""
        policy = self._decomposition_policy
        if policy is None:
            return selection

        from core.ports.decomposition_policy_port import InitialDecomposition, InitialSubtask

        flattened: list[InitialSubtask] = []
        entries: list[tuple[int, ...]] = []
        sinks: list[tuple[int, ...]] = []

        for group in selection.subtasks:
            nested = policy.select_initial(
                parent_task_description=group.description,
                domain_context=domain_context,
                is_root=False,
                redecomposition_count=0,
            )
            children = nested.subtasks if nested is not None else (
                replace(group, depends_on=()),
            )
            self._validate_dependency_indices(children)
            offset = len(flattened)
            depended_on = {dependency for child in children for dependency in child.depends_on}
            local_entries = tuple(
                offset + index for index, child in enumerate(children) if not child.depends_on
            )
            local_sinks = tuple(
                offset + index for index in range(len(children)) if index not in depended_on
            )
            entries.append(local_entries)
            sinks.append(local_sinks)
            flattened.extend(
                replace(
                    child,
                    depends_on=tuple(offset + dependency for dependency in child.depends_on),
                )
                for child in children
            )

        self._validate_dependency_indices(selection.subtasks)
        dependencies = [set(child.depends_on) for child in flattened]
        for group_index, group in enumerate(selection.subtasks):
            for dependency_group in group.depends_on:
                for entry_index in entries[group_index]:
                    dependencies[entry_index].update(sinks[dependency_group])
        flattened = [
            replace(child, depends_on=tuple(sorted(dependencies[index])))
            for index, child in enumerate(flattened)
        ]
        return InitialDecomposition(
            subtasks=tuple(flattened),
            policy_version=selection.policy_version,
            phase=selection.phase,
            route=selection.route,
            triggers=(*selection.triggers, "manager_layer_omitted"),
            evidence_references=selection.evidence_references,
        )

    @staticmethod
    def _validate_dependency_indices(subtasks: tuple[InitialSubtask, ...]) -> None:
        for index, subtask in enumerate(subtasks):
            invalid = [dependency for dependency in subtask.depends_on if not 0 <= dependency < index]
            if invalid:
                raise ValueError(
                    f"initial decomposition subtask {index} has invalid dependencies: {invalid}"
                )

    def role_history(self, agent: AgentSession) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Previously selected, failed, and completed role labels for this tree."""
        factory = self._child_factory
        previously = tuple(sorted(factory.get_tree_role_prefixes(agent.agent_id)))
        failed = tuple(sorted(factory.get_failed_role_prefixes(agent.agent_id)))
        completed = tuple(sorted(factory.get_completed_role_prefixes(agent.agent_id)))
        # Also derive failed labels from the agent's failure_history (authoritative
        # for the current re-decomposition attempt even if registry lag exists).
        history_failed = tuple(
            label
            for record in agent.failure_history
            if (label := self._extract_role_prefix(record.child_task or "")) is not None
        )
        if history_failed:
            failed = tuple(sorted(set(failed) | set(history_failed)))
        return previously, failed, completed

    def failure_evidence_references(self, agent: AgentSession) -> list[str]:
        """Stable references for failed children feeding a re-decomposition route."""
        refs: list[str] = []
        for record in agent.failure_history:
            refs.append(f"child_failed:{record.child_id}")
        return refs

    async def enforce_decomposition_contract(
        self,
        agent: AgentSession,
        subtasks: list[Subtask],
        dedup_map: dict[int, int] | None = None,
    ) -> tuple[list[Subtask] | None, DecompositionRoute | None]:
        """Apply the domain role contract; return (corrected, optional route provenance).

        Returns ``(None, None)`` if the repair cannot fit hierarchy limits.
        """
        if self._decomposition_validator is None:
            return subtasks, None
        domain_context = agent.hierarchy_limits.domain_context if agent.hierarchy_limits else None
        previously, failed, completed = self.role_history(agent)
        verdict = self._decomposition_validator.classify(
            parent_task_description=agent.task_description,
            subtask_descriptions=tuple(st.description for st in subtasks),
            domain_context=domain_context,
            redecomposition_count=agent.redecomposition_count,
            previously_selected_roles=previously,
            failed_role_labels=failed,
            completed_role_labels=completed,
            redecomposition_limit=self._max_redecompositions,
        )

        route_provenance: DecompositionRoute | None = None
        if verdict.route is not None:
            from core.ports.decomposition_validator_port import DecompositionRoute

            route_provenance = DecompositionRoute(
                policy_version=verdict.policy_version,
                phase=verdict.phase,
                route=verdict.route,
                triggers=verdict.triggers,
                evidence_references=verdict.evidence_references
                or tuple(self.failure_evidence_references(agent)),
                remaining_budget=verdict.remaining_budget,
            )

        route_evidence = (
            route_provenance.evidence_references if route_provenance is not None else ()
        )
        subtasks = self._apply_validated_leaf_contract(
            agent,
            subtasks,
            verdict.validated_subtasks,
            default_evidence=route_evidence,
        )

        if verdict.ok and not verdict.removals and not verdict.additions:
            resolved = self.resolve_catalog_dependencies(subtasks, dedup_map)
            return resolved, route_provenance

        from core.domain.values.subtask import Subtask

        removals = set(verdict.removals)
        base_config = (
            agent.config.model_dump()
            if route_provenance is not None or verdict.validated_subtasks
            else (dict(subtasks[0].config) if subtasks else {})
        )
        repair_evidence = route_evidence or tuple(self.failure_evidence_references(agent))
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
                evidence_references=repair_evidence,
                selection_source="host_repair",
            )
            for add in verdict.additions
        )
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

        failure = await self.check_limit_violations(agent, corrected)
        if failure:
            agent.fail_with_reason(
                f"decomposition contract unsatisfiable within hierarchy limits: {failure}"
            )
            return None, None

        return corrected, route_provenance

    @staticmethod
    def _apply_validated_leaf_contract(
        agent: AgentSession,
        subtasks: list[Subtask],
        validated_subtasks: tuple[ValidatedSubtask, ...],
        *,
        default_evidence: tuple[str, ...],
    ) -> list[Subtask]:
        """Bind trusted structure while preserving Manager-authored task content."""
        canonical = {
            item.subtask_index: item.canonical_description for item in validated_subtasks
        }
        trusted_config = agent.config.model_dump()
        normalized: list[Subtask] = []
        for index, subtask in enumerate(subtasks):
            description = canonical.get(index)
            if description is None:
                normalized.append(subtask)
                continue
            normalized.append(
                subtask.model_copy(
                    update={
                        "description": description,
                        "config": dict(trusted_config),
                        "estimated_complexity": "simple",
                        "criticality": "required",
                        "dependency_type": "finish_to_start",
                        "dependency_failure_policy": "block",
                        "evidence_references": (
                            subtask.evidence_references or default_evidence
                        ),
                        "execution_mode": "auto",
                        "procedure_ref": "",
                        "procedure_params": {},
                    }
                )
            )
        return normalized

    def resolve_catalog_dependencies(
        self, subtasks: list[Subtask], old_to_new: dict[int, int] | None = None
    ) -> list[Subtask]:
        """Set each leaf's depends_on to its catalog HARD producers."""
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
        """Check hard limits. Returns failure reason or None."""
        limits = agent.hierarchy_limits
        violations: list[dict[str, Any]] = []

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
        """Remove subtasks whose roles are completed or already covered (not failed).

        Completed roles are always forbidden. Failed roles may be reissued.
        Unrelated tree-wide duplicates remain forbidden.
        """
        if agent.parent_id is None:
            return subtasks  # Boss-level decomposition — no siblings to clash with

        uncle_roles = self._child_factory.get_sibling_role_prefixes(
            agent.agent_id, agent.parent_id
        )
        tree_roles = self._child_factory.get_tree_role_prefixes(agent.agent_id)
        completed = self._child_factory.get_completed_role_prefixes(agent.agent_id)
        failed = self._child_factory.get_failed_role_prefixes(agent.agent_id)
        # Failure history is authoritative for this parent's re-decomposition.
        for record in agent.failure_history:
            label = self._extract_role_prefix(record.child_task or "")
            if label:
                failed = failed | {label}

        uncle_roles = {role.casefold() for role in uncle_roles}
        tree_roles = {role.casefold() for role in tree_roles}
        completed = {role.casefold() for role in completed}
        failed = {role.casefold() for role in failed}

        if not uncle_roles and not tree_roles and not completed:
            return subtasks

        # The registry excludes this grouping agent itself but retains every
        # other same-labelled agent. A failed leaf may be deliberately reissued.
        used = uncle_roles | tree_roles
        forbidden = (used - failed) | completed

        kept: list[Subtask] = []
        for st in subtasks:
            prefix = self._extract_role_prefix(st.description)
            normalized_prefix = prefix.casefold() if prefix else None
            if normalized_prefix and normalized_prefix in forbidden:
                logger.info(
                    "Agent %s: stripping duplicate subtask [%s] "
                    "(completed or already covered by another agent)",
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
