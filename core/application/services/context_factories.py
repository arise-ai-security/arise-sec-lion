"""Factory functions for building context data from domain objects.

This module provides convenience functions to construct ContextData instances
from domain aggregates (AgentSession, SharedExecutionContext).

These factories:
- Encapsulate the mapping from domain to context types
- Handle truncation and summarization
- Provide consistent formatting across the codebase

Usage:
    from core.application.services.context_factories import (
        parent_summary_from_agent,
        sibling_results_from_agents,
        shared_decisions_from_context,
    )

    context = ContextComposer()
    context.add(parent_summary_from_agent(parent_agent))
    context.add(sibling_results_from_agents(siblings))
"""

from typing import TYPE_CHECKING
from uuid import UUID

from core.domain.values.context.data_types import (
    AncestorData,
    AncestorEntry,
    AncestryChain,
    ArtifactEntry,
    ChildOutcomeEntry,
    ChildOutcomes,
    DecisionEntry,
    ParentSummary,
    SharedArtifacts,
    SharedDecisions,
    SiblingEntry,
    SiblingResults,
    SupervisorExpectations,
)
from core.domain.values.subtask import SubtaskJustification

if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.shared_context import SharedExecutionContext


# =============================================================================
# Default Truncation Limits
# =============================================================================

DEFAULT_TASK_SUMMARY_LIMIT = 200
DEFAULT_RESULT_SUMMARY_LIMIT = 500
DEFAULT_ANCESTRY_TASK_LIMIT = 100


# =============================================================================
# Hierarchical Context Factories
# =============================================================================


def parent_summary_from_agent(
    agent: "AgentSession",
    result_limit: int = DEFAULT_RESULT_SUMMARY_LIMIT,
) -> ParentSummary:
    """Build ParentSummary from an AgentSession.

    Args:
        agent: The parent agent to summarize.
        result_limit: Maximum characters for result summary.

    Returns:
        ParentSummary value object.

    Example:
        parent = await repository.load(agent.parent_id)
        context.add(parent_summary_from_agent(parent))
    """
    result_summary = None
    if agent.result:
        result_summary = agent.result[:result_limit]

    return ParentSummary(
        task=agent.task_description or "",
        result=result_summary,
        decisions=tuple(agent.local_decisions),
        role=agent.role.value,
    )


def ancestor_data_from_agent(
    agent: "AgentSession",
    label: str,
    depth: int = 0,
    result_limit: int = DEFAULT_RESULT_SUMMARY_LIMIT,
) -> AncestorData:
    """Build AncestorData from an AgentSession with custom label.

    Use this when you need context from a specific ancestor,
    identified by a label (e.g., "fixer", "boss_goal").

    Args:
        agent: The ancestor agent.
        label: User-defined label (becomes part of template key).
        depth: Depth of this ancestor in the hierarchy (0 = root).
        result_limit: Maximum characters for result summary.

    Returns:
        AncestorData value object.

    Example:
        fixer = await find_ancestor_by_role(agent, "fixer")
        context.add(ancestor_data_from_agent(fixer, label="fixer", depth=1))
    """
    result_summary = None
    if agent.result:
        result_summary = agent.result[:result_limit]

    return AncestorData(
        label=label,
        agent_id=agent.agent_id,
        role=agent.role.value,
        task=agent.task_description or "",
        result=result_summary,
        decisions=tuple(agent.local_decisions),
        depth=depth,
    )


def ancestry_chain_from_agents(
    agents: list["AgentSession"],
    task_limit: int = DEFAULT_ANCESTRY_TASK_LIMIT,
) -> AncestryChain:
    """Build AncestryChain from list of ancestor agents.

    Args:
        agents: List of ancestors ordered from root (depth=0) to immediate parent.
        task_limit: Maximum characters for task summary.

    Returns:
        AncestryChain value object.

    Example:
        ancestors = await get_ancestry_chain(agent)  # [boss, manager, ...]
        context.add(ancestry_chain_from_agents(ancestors))
    """
    entries = []
    for i, agent in enumerate(agents):
        task_summary = (agent.task_description or "")[:task_limit]
        entries.append(
            AncestorEntry(
                agent_id=str(agent.agent_id),
                role=agent.role.value,
                task_summary=task_summary,
                depth=i,
            )
        )
    return AncestryChain(ancestors=tuple(entries))


# =============================================================================
# Horizontal Context Factories
# =============================================================================


def sibling_results_from_agents(
    siblings: list["AgentSession"],
    exclude_id: UUID | None = None,
    task_limit: int = DEFAULT_TASK_SUMMARY_LIMIT,
    result_limit: int = DEFAULT_RESULT_SUMMARY_LIMIT,
) -> SiblingResults:
    """Build SiblingResults from list of sibling agents.

    Args:
        siblings: List of sibling agents (including self).
        exclude_id: Agent ID to exclude (typically current agent).
        task_limit: Maximum characters for task summary.
        result_limit: Maximum characters for result summary.

    Returns:
        SiblingResults value object.

    Example:
        siblings = await get_siblings(agent)
        context.add(sibling_results_from_agents(
            siblings,
            exclude_id=agent.agent_id,
        ))
    """
    entries = []
    for sibling in siblings:
        if exclude_id and sibling.agent_id == exclude_id:
            continue

        task_summary = (sibling.task_description or "")[:task_limit]
        result_summary = None
        if sibling.result:
            result_summary = sibling.result[:result_limit]

        entries.append(
            SiblingEntry(
                agent_id=str(sibling.agent_id),
                index=sibling.sibling_index,
                status=sibling.status.value,
                task_summary=task_summary,
                result_summary=result_summary,
            )
        )

    # Sort by sibling index for consistent ordering
    entries.sort(key=lambda e: e.index)
    return SiblingResults(siblings=tuple(entries))


def sibling_entry_from_agent(
    agent: "AgentSession",
    task_limit: int = DEFAULT_TASK_SUMMARY_LIMIT,
    result_limit: int = DEFAULT_RESULT_SUMMARY_LIMIT,
) -> SiblingEntry:
    """Build a single SiblingEntry from an AgentSession.

    Useful when building sibling lists incrementally.

    Args:
        agent: The sibling agent.
        task_limit: Maximum characters for task summary.
        result_limit: Maximum characters for result summary.

    Returns:
        SiblingEntry value object.
    """
    task_summary = (agent.task_description or "")[:task_limit]
    result_summary = None
    if agent.result:
        result_summary = agent.result[:result_limit]

    return SiblingEntry(
        agent_id=str(agent.agent_id),
        index=agent.sibling_index,
        status=agent.status.value,
        task_summary=task_summary,
        result_summary=result_summary,
    )


# =============================================================================
# Shared Context Factories
# =============================================================================


def shared_decisions_from_context(
    context: "SharedExecutionContext",
    keys: list[str] | None = None,
) -> SharedDecisions:
    """Build SharedDecisions from SharedExecutionContext.

    Args:
        context: The shared execution context.
        keys: Optional list of decision keys to include (None = all).

    Returns:
        SharedDecisions value object.

    Example:
        shared_ctx = await shared_context_port.get(root_id)
        context.add(shared_decisions_from_context(shared_ctx))
    """
    decision_keys = keys if keys else context.list_decisions()
    entries = []

    for key in decision_keys:
        decision = context.get_decision(key)
        if decision:
            entries.append(
                DecisionEntry(
                    key=decision.key,
                    value=decision.value,
                    rationale=decision.rationale,
                    decided_by=str(decision.decided_by),
                )
            )

    return SharedDecisions(decisions=tuple(entries))


def shared_artifacts_from_context(
    context: "SharedExecutionContext",
    keys: list[str] | None = None,
    include_content: bool = True,
    content_limit: int = 1000,
) -> SharedArtifacts:
    """Build SharedArtifacts from SharedExecutionContext.

    Args:
        context: The shared execution context.
        keys: Optional list of artifact keys to include (None = all).
        include_content: Whether to include artifact content.
        content_limit: Maximum characters for content (if included).

    Returns:
        SharedArtifacts value object.

    Example:
        shared_ctx = await shared_context_port.get(root_id)
        context.add(shared_artifacts_from_context(
            shared_ctx,
            keys=["vulnerability_report", "poc_code"],
        ))
    """
    artifact_keys = keys if keys else context.list_artifacts()
    entries = []

    for key in artifact_keys:
        artifact = context.get_artifact(key)
        if artifact:
            content = None
            if include_content and artifact.content:
                content = artifact.content[:content_limit]

            entries.append(
                ArtifactEntry(
                    key=artifact.key,
                    content_type=artifact.content_type,
                    content=content,
                    stored_by=str(artifact.stored_by),
                )
            )

    return SharedArtifacts(artifacts=tuple(entries))


def decision_entry_from_shared(
    key: str,
    context: "SharedExecutionContext",
) -> DecisionEntry | None:
    """Build a single DecisionEntry from SharedExecutionContext.

    Args:
        key: The decision key to look up.
        context: The shared execution context.

    Returns:
        DecisionEntry if found, None otherwise.
    """
    decision = context.get_decision(key)
    if decision is None:
        return None

    return DecisionEntry(
        key=decision.key,
        value=decision.value,
        rationale=decision.rationale,
        decided_by=str(decision.decided_by),
    )


def artifact_entry_from_shared(
    key: str,
    context: "SharedExecutionContext",
    content_limit: int = 1000,
) -> ArtifactEntry | None:
    """Build a single ArtifactEntry from SharedExecutionContext.

    Args:
        key: The artifact key to look up.
        context: The shared execution context.
        content_limit: Maximum characters for content.

    Returns:
        ArtifactEntry if found, None otherwise.
    """
    artifact = context.get_artifact(key)
    if artifact is None:
        return None

    content = None
    if artifact.content:
        content = artifact.content[:content_limit]

    return ArtifactEntry(
        key=artifact.key,
        content_type=artifact.content_type,
        content=content,
        stored_by=str(artifact.stored_by),
    )


# =============================================================================
# Child Outcomes Factories
# =============================================================================


def child_outcomes_from_agent(
    agent: "AgentSession",
    result_limit: int = DEFAULT_RESULT_SUMMARY_LIMIT,
    task_limit: int = DEFAULT_TASK_SUMMARY_LIMIT,
) -> ChildOutcomes:
    """Build ChildOutcomes from parent agent's structured_task_outcomes.

    Creates a renderable context object containing all children's TaskOutcome
    data, suitable for inclusion in parent's prompt during re-evaluation
    or aggregation phases.

    Args:
        agent: The parent agent with completed children.
        result_limit: Maximum characters for result text.
        task_limit: Maximum characters for task summary.

    Returns:
        ChildOutcomes value object.

    Example:
        # In a Pipeline step for Manager/Boss aggregation
        if agent.structured_task_outcomes:
            context.add(child_outcomes_from_agent(agent))
    """
    entries = []

    for child_id, outcome in agent.structured_task_outcomes.items():
        task_summary = outcome.task_summary[:task_limit] if outcome.task_summary else ""
        result_text = outcome.result_text[:result_limit] if outcome.result_text else ""

        entries.append(
            ChildOutcomeEntry(
                child_id=str(child_id),
                task_summary=task_summary,
                result_text=result_text,
                artifacts=outcome.artifacts,
                decisions=outcome.decisions,
                status="completed",
            )
        )

    return ChildOutcomes(outcomes=tuple(entries))


# =============================================================================
# Supervisor Expectations Factories (Design Choice 4)
# =============================================================================


def supervisor_expectations_from_justification(
    parent_task: str,
    justification: SubtaskJustification,
    child_budget: float | None = None,
    parent_budget: float | None = None,
    budget_weight: float | None = None,
    total_weights: float | None = None,
    num_siblings: int | None = None,
) -> SupervisorExpectations:
    """Build SupervisorExpectations from parent task and subtask justification.

    Creates context for a child agent that explains why they were assigned
    this task and what their supervisor expects. Includes budget allocation
    context when available.

    Args:
        parent_task: The supervisor's (parent's) original task description.
        justification: The SubtaskJustification from task decomposition.
        child_budget: The budget allocated to this child (optional).
        parent_budget: The parent's total budget (optional).
        budget_weight: This child's budget weight (optional).
        total_weights: Sum of all sibling weights (optional).
        num_siblings: Total number of sibling tasks (optional).

    Returns:
        SupervisorExpectations value object.

    Example:
        # During child agent creation
        context.add(supervisor_expectations_from_justification(
            parent_task=parent_agent.task_description,
            justification=subtask.justification,
            child_budget=182.0,
            parent_budget=1000.0,
            budget_weight=1.0,
            total_weights=5.5,
            num_siblings=3,
        ))
    """
    # Build budget allocation string if budget info is available
    budget_allocation = justification.budget_allocation
    if not budget_allocation and child_budget is not None and parent_budget is not None:
        pct = (child_budget / parent_budget * 100) if parent_budget > 0 else 0
        if budget_weight is not None and total_weights is not None:
            budget_allocation = (
                f"{pct:.0f}% of budget "
                f"(weight {budget_weight:.1f} of {total_weights:.1f}"
                + (f" across {num_siblings} subtasks)" if num_siblings else ")")
            )
        else:
            budget_allocation = f"{pct:.0f}% of budget ({child_budget:.0f} of {parent_budget:.0f})"

    return SupervisorExpectations(
        supervisor_task=parent_task or "",
        objective=justification.objective,
        split_reason=justification.split_reason,
        suggested_approach=justification.plan,
        why_it_works=justification.why_it_works,
        expected_deliverables=justification.expected_results,
        budget_allocation=budget_allocation,
        complexity_assessment=justification.complexity_assessment,
        significance=justification.significance_weight,
        resource_justification=justification.resource_justification,
    )


def supervisor_expectations_from_agent(
    parent_agent: "AgentSession",
    justification: SubtaskJustification,
    child_budget: float | None = None,
    budget_weight: float | None = None,
    total_weights: float | None = None,
    num_siblings: int | None = None,
    task_limit: int = DEFAULT_TASK_SUMMARY_LIMIT,
) -> SupervisorExpectations:
    """Build SupervisorExpectations from parent agent and justification.

    Convenience wrapper that extracts parent task and budget from AgentSession.

    Args:
        parent_agent: The parent (supervisor) agent.
        justification: The SubtaskJustification from task decomposition.
        child_budget: The budget allocated to this child (optional).
        budget_weight: This child's budget weight (optional).
        total_weights: Sum of all sibling weights (optional).
        num_siblings: Total number of sibling tasks (optional).
        task_limit: Maximum characters for parent task summary.

    Returns:
        SupervisorExpectations value object.

    Example:
        # In ChildAgentFactory
        context.add(supervisor_expectations_from_agent(
            parent_agent=parent,
            justification=subtask.justification,
            child_budget=child.complexity_budget,
            budget_weight=subtask.budget_weight,
            total_weights=sum(s.budget_weight for s in subtasks),
            num_siblings=len(subtasks),
        ))
    """
    parent_task = (parent_agent.task_description or "")[:task_limit]
    parent_budget = parent_agent.complexity_budget if parent_agent.complexity_budget > 0 else None

    return supervisor_expectations_from_justification(
        parent_task=parent_task,
        justification=justification,
        child_budget=child_budget,
        parent_budget=parent_budget,
        budget_weight=budget_weight,
        total_weights=total_weights,
        num_siblings=num_siblings,
    )
