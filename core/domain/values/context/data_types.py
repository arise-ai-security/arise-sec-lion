"""Concrete context data types for prompt composition.

This module provides type-safe value objects for different kinds of context
data that can be passed to agent prompts via the ContextComposer.

All types implement the ContextData protocol and are immutable (frozen).

Categories:
- Hierarchical: ParentSummary, AncestorData, AncestryChain
- Horizontal: SiblingResults, SiblingEntry
- Shared: SharedDecisions, SharedArtifacts
- Custom: CustomContext for arbitrary user data
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel


# =============================================================================
# Hierarchical Context (Parent/Ancestor Data)
# =============================================================================


class ParentSummary(BaseModel):
    """Summary of parent node's task and result.

    Use when a child needs context about its immediate parent's work.

    Example:
        context.add(ParentSummary(
            task="Analyze vulnerability in auth module",
            result="Found SQL injection in login handler",
            decisions=("use_parameterized_queries",),
            role="manager",
        ))
    """

    model_config = {"frozen": True}

    task: str
    result: str | None = None
    decisions: tuple[str, ...] = ()
    role: str = ""

    @property
    def template_key(self) -> str:
        return "parent_summary"

    def to_template_dict(self) -> dict[str, Any]:
        return self.model_dump()


class AncestorEntry(BaseModel):
    """Single ancestor in an ancestry chain.

    Lightweight representation for ancestry traversal.
    """

    model_config = {"frozen": True}

    agent_id: str
    role: str
    task_summary: str
    depth: int


class AncestryChain(BaseModel):
    """Full ancestry from current node to root.

    Use when an agent needs to understand its full lineage.

    Example:
        context.add(AncestryChain(ancestors=(
            AncestorEntry(agent_id="...", role="boss", task_summary="Fix CVE-2023-1234", depth=0),
            AncestorEntry(agent_id="...", role="manager", task_summary="Develop patch", depth=1),
        )))
    """

    model_config = {"frozen": True}

    ancestors: tuple[AncestorEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "ancestry_chain"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "ancestors": [a.model_dump() for a in self.ancestors],
            "depth": len(self.ancestors),
        }


class AncestorData(BaseModel):
    """Data from a specific ancestor node.

    Use when you need detailed context from a specific ancestor,
    not just the immediate parent. The label identifies which
    ancestor this is (e.g., "fixer_manager", "boss_goal").

    Example:
        # Get the fixer manager's context for a worker
        context.add(AncestorData(
            label="fixer",
            agent_id=fixer.agent_id,
            role="manager",
            task="Apply security patch to auth module",
            result="Patch strategy: input validation + parameterized queries",
            decisions=("validate_all_inputs",),
            depth=1,
        ))
    """

    model_config = {"frozen": True}

    label: str  # User-defined label (e.g., "fixer_context", "boss_goal")
    agent_id: UUID
    role: str
    task: str
    result: str | None = None
    decisions: tuple[str, ...] = ()
    depth: int = 0

    @property
    def template_key(self) -> str:
        return f"ancestor_{self.label}"

    def to_template_dict(self) -> dict[str, Any]:
        return self.model_dump()


# =============================================================================
# Horizontal Context (Sibling Data)
# =============================================================================


class SiblingEntry(BaseModel):
    """Single sibling entry with status and optional result.

    Represents a sibling worker's current state.
    """

    model_config = {"frozen": True}

    agent_id: str
    index: int
    status: str  # pending, analyzing, in_progress, completed, failed
    task_summary: str
    result_summary: str | None = None


class SiblingResults(BaseModel):
    """Results from sibling workers.

    Use for worker coordination - seeing what siblings have done.

    Example:
        context.add(SiblingResults(siblings=(
            SiblingEntry(
                agent_id="...",
                index=0,
                status="completed",
                task_summary="Set up test environment",
                result_summary="Docker container running on port 8080",
            ),
            SiblingEntry(
                agent_id="...",
                index=1,
                status="in_progress",
                task_summary="Run exploit PoC",
            ),
        )))
    """

    model_config = {"frozen": True}

    siblings: tuple[SiblingEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "sibling_results"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "siblings": [s.model_dump() for s in self.siblings],
            "total_count": len(self.siblings),
            "completed_count": sum(1 for s in self.siblings if s.status == "completed"),
            "in_progress_count": sum(
                1 for s in self.siblings if s.status in ("analyzing", "in_progress")
            ),
        }


# =============================================================================
# Shared Context (Global Data)
# =============================================================================


class DecisionEntry(BaseModel):
    """Single decision entry from SharedExecutionContext."""

    model_config = {"frozen": True}

    key: str
    value: str
    rationale: str = ""
    decided_by: str = ""


class SharedDecisions(BaseModel):
    """Decisions from SharedExecutionContext.

    Use to share architectural/design decisions across agents.

    Example:
        context.add(SharedDecisions(decisions=(
            DecisionEntry(
                key="target_framework",
                value="django",
                rationale="Target uses Django 3.2",
                decided_by="manager-123",
            ),
        )))
    """

    model_config = {"frozen": True}

    decisions: tuple[DecisionEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "shared_decisions"

    def to_template_dict(self) -> dict[str, Any]:
        return {"decisions": [d.model_dump() for d in self.decisions]}


class ArtifactEntry(BaseModel):
    """Single artifact entry from SharedExecutionContext."""

    model_config = {"frozen": True}

    key: str
    content_type: str
    content: str | None = None
    stored_by: str = ""


class SharedArtifacts(BaseModel):
    """Artifacts from SharedExecutionContext.

    Use to share produced outputs (files, analysis results) across agents.

    Example:
        context.add(SharedArtifacts(artifacts=(
            ArtifactEntry(
                key="vulnerability_report",
                content_type="text/markdown",
                content="## Findings\\n- SQL Injection in login.php",
                stored_by="worker-456",
            ),
        )))
    """

    model_config = {"frozen": True}

    artifacts: tuple[ArtifactEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "shared_artifacts"

    def to_template_dict(self) -> dict[str, Any]:
        return {"artifacts": [a.model_dump() for a in self.artifacts]}


# =============================================================================
# Child Outcomes (Parent viewing children's results)
# =============================================================================


class ChildOutcomeEntry(BaseModel):
    """Single child's task outcome.

    Represents a completed child's structured result for parent consumption.
    """

    model_config = {"frozen": True}

    child_id: str
    task_summary: str
    result_text: str
    artifacts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    status: str = "completed"  # completed, failed


class ChildOutcomes(BaseModel):
    """Structured outcomes from child agents.

    Use when a parent (Manager/Boss) needs to see what children produced.
    This renders TaskOutcome data to prompts for parent re-evaluation
    or aggregation decisions.

    Example:
        context.add(ChildOutcomes(outcomes=(
            ChildOutcomeEntry(
                child_id="worker-123",
                task_summary="Develop exploit PoC",
                result_text="Created poc.py with buffer overflow...",
                artifacts=("poc.py", "debug_log.txt"),
                decisions=("use_ret2libc",),
            ),
        )))
    """

    model_config = {"frozen": True}

    outcomes: tuple[ChildOutcomeEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "child_outcomes"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "outcomes": [o.model_dump() for o in self.outcomes],
            "total_count": len(self.outcomes),
            "completed_count": sum(1 for o in self.outcomes if o.status == "completed"),
            "failed_count": sum(1 for o in self.outcomes if o.status == "failed"),
        }


# =============================================================================
# Custom Context (User-Defined)
# =============================================================================


class CustomContext(BaseModel):
    """User-defined custom context with arbitrary data.

    Use for domain-specific context that doesn't fit standard types.
    The label becomes part of the template key: custom_{label}.

    Example:
        context.add(CustomContext(
            label="vulnerability_info",
            data={
                "cve_id": "CVE-2023-1234",
                "affected_function": "parse_input()",
                "exploit_type": "buffer_overflow",
            },
        ))

        # In template:
        # {% if custom_vulnerability_info %}
        # CVE: {{ custom_vulnerability_info.cve_id }}
        # {% endif %}
    """

    model_config = {"frozen": True}

    label: str
    data: dict[str, Any]

    @property
    def template_key(self) -> str:
        return f"custom_{self.label}"

    def to_template_dict(self) -> dict[str, Any]:
        return self.data


# =============================================================================
# Depth-Targeted Summary (Worker → Specific Ancestor by Depth)
# =============================================================================


class DepthTargetedSummaryEntry(BaseModel):
    """Single depth-targeted worker summary entry.

    Represents a summary message from a worker to an ancestor at a specific depth.
    """

    model_config = {"frozen": True}

    worker_id: str
    worker_task: str
    summary_text: str
    source_depth: int  # Depth of the worker
    timestamp: str = ""


class DepthTargetedSummary(BaseModel):
    """Worker summaries targeted at a specific ancestor depth.

    Use when workers need to send their summary to an ancestor at a
    specific hierarchy depth. Common use case: workers report to the
    first child of BOSS (depth=1), which acts as a coordinator.

    Depth mapping:
    - depth=0: BOSS (root)
    - depth=1: First child of BOSS (e.g., main coordinator/manager)
    - depth=N: Ancestor at that specific depth

    Example:
        # Worker broadcasts summary to depth=1 ancestor
        context.add(DepthTargetedSummary(
            target_depth=1,
            summaries=(
                DepthTargetedSummaryEntry(
                    worker_id="worker-123",
                    worker_task="Scan auth module",
                    summary_text="Found 3 vulnerabilities...",
                    source_depth=3,  # Worker is at depth 3
                ),
            ),
        ))

        # In template:
        # {% if depth_targeted_summaries %}
        # <DEPTH_SUMMARIES target_depth="{{ depth_targeted_summaries.target_depth }}">
        # {% for summary in depth_targeted_summaries.summaries %}
        # <summary from="{{ summary.worker_id }}" source_depth="{{ summary.source_depth }}">
        #   {{ summary.summary_text }}
        # </summary>
        # {% endfor %}
        # </DEPTH_SUMMARIES>
        # {% endif %}
    """

    model_config = {"frozen": True}

    target_depth: int = 1  # Default to first child of boss
    summaries: tuple[DepthTargetedSummaryEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "depth_targeted_summaries"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "target_depth": self.target_depth,
            "summaries": [s.model_dump() for s in self.summaries],
            "total_count": len(self.summaries),
        }




