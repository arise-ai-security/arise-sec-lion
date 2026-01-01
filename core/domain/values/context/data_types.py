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
# Bidirectional Context Types (Parent↔Child, Sibling↔Sibling)
# =============================================================================


class ParentGuidance(BaseModel):
    """Strategic guidance from parent to child (Parent → Child).

    Use when parents need to pass strategic direction, constraints,
    or context that influences how children approach their tasks.
    This supplements the task description with structured guidance.

    HOW TO ADD: Include in SpawnPayload when creating children, then
    extract and add to ContextComposer in child's prompt building step.

    Example:
        # Parent adds guidance to spawn payload
        guidance = ParentGuidance(
            strategy="defensive",
            priority_targets=("authentication", "session_management"),
            constraints={"max_time_per_target": 300},
            notes="Focus on OWASP Top 10 vulnerabilities",
        )

        # Child extracts and adds to context
        context.add(guidance)

        # In child's template:
        # {% if parent_guidance %}
        # Strategy: {{ parent_guidance.strategy }}
        # Priority targets: {{ parent_guidance.priority_targets | join(", ") }}
        # Notes: {{ parent_guidance.notes }}
        # {% endif %}
    """

    model_config = {"frozen": True}

    strategy: str = ""
    priority_targets: tuple[str, ...] = ()
    constraints: dict[str, Any] = {}
    notes: str = ""

    @property
    def template_key(self) -> str:
        return "parent_guidance"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "priority_targets": list(self.priority_targets),
            "constraints": self.constraints,
            "notes": self.notes,
        }


class ChildFeedback(BaseModel):
    """Feedback from child to parent (Child → Parent).

    Use when children need to report back structured feedback beyond
    just the result text. Store in SharedExecutionContext as artifact.

    HOW TO ADD: Child stores as artifact on completion, parent retrieves
    via factory function when building aggregation prompt.

    Example:
        # Child stores feedback
        feedback = ChildFeedback(
            child_id=str(agent.agent_id),
            success_level="partial",
            blockers=("firewall_detected", "rate_limited"),
            recommendations=("try_alternative_port", "use_proxy"),
            confidence_score=0.7,
        )
        # Store as artifact in SharedExecutionContext

        # Parent retrieves
        context.add(child_feedbacks_from_context(shared_ctx))

        # In parent's template:
        # {% if child_feedbacks %}
        # {% for fb in child_feedbacks.feedbacks %}
        # Child {{ fb.child_id }}: {{ fb.success_level }}
        # Blockers: {{ fb.blockers | join(", ") }}
        # {% endfor %}
        # {% endif %}
    """

    model_config = {"frozen": True}

    child_id: str = ""
    success_level: str = "unknown"  # "full", "partial", "failed", "unknown"
    blockers: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    confidence_score: float = 0.0
    metadata: dict[str, Any] = {}

    @property
    def template_key(self) -> str:
        return "child_feedback"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "success_level": self.success_level,
            "blockers": list(self.blockers),
            "recommendations": list(self.recommendations),
            "confidence_score": self.confidence_score,
            "metadata": self.metadata,
        }


class ChildFeedbackCollection(BaseModel):
    """Collection of feedback from multiple children (Child → Parent).

    Aggregates ChildFeedback from all completed children for the parent
    to review during aggregation or re-evaluation.

    Example:
        context.add(ChildFeedbackCollection(
            feedbacks=(
                ChildFeedback(child_id="child-1", success_level="full", ...),
                ChildFeedback(child_id="child-2", success_level="partial", ...),
            ),
        ))
    """

    model_config = {"frozen": True}

    feedbacks: tuple[ChildFeedback, ...] = ()

    @property
    def template_key(self) -> str:
        return "child_feedbacks"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "feedbacks": [f.to_template_dict() for f in self.feedbacks],
            "total_count": len(self.feedbacks),
            "success_count": sum(1 for f in self.feedbacks if f.success_level == "full"),
            "partial_count": sum(1 for f in self.feedbacks if f.success_level == "partial"),
            "failed_count": sum(1 for f in self.feedbacks if f.success_level == "failed"),
        }


class SiblingCoordination(BaseModel):
    """Coordination data shared between siblings (Sibling ↔ Sibling).

    Use when siblings need to coordinate their work to avoid conflicts,
    share discovered information, or claim resources.

    HOW TO ADD: Store in SharedExecutionContext as sibling works,
    other siblings retrieve via factory function.

    Example:
        # Sibling claims targets and shares discoveries
        coord = SiblingCoordination(
            sibling_id=str(agent.agent_id),
            claimed_targets=("port_80", "port_443"),
            discovered_info={"admin_panel": "/admin", "api_version": "v2"},
            warnings=("rate_limit_approaching",),
            available_for_help=True,
        )
        # Store as artifact in SharedExecutionContext

        # Other sibling retrieves
        context.add(sibling_coordinations_from_context(shared_ctx))

        # In sibling's template:
        # {% if sibling_coordinations %}
        # Already claimed by siblings: {{ sibling_coordinations.all_claimed_targets | join(", ") }}
        # Shared discoveries:
        # {% for key, value in sibling_coordinations.all_discovered_info.items() %}
        #   {{ key }}: {{ value }}
        # {% endfor %}
        # {% endif %}
    """

    model_config = {"frozen": True}

    sibling_id: str = ""
    claimed_targets: tuple[str, ...] = ()
    discovered_info: dict[str, Any] = {}
    warnings: tuple[str, ...] = ()
    available_for_help: bool = False

    @property
    def template_key(self) -> str:
        return "sibling_coordination"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "sibling_id": self.sibling_id,
            "claimed_targets": list(self.claimed_targets),
            "discovered_info": self.discovered_info,
            "warnings": list(self.warnings),
            "available_for_help": self.available_for_help,
        }


class SiblingCoordinationCollection(BaseModel):
    """Collection of coordination data from all siblings.

    Aggregates SiblingCoordination from all siblings for coordinated
    task execution.

    Example:
        context.add(SiblingCoordinationCollection(
            coordinations=(
                SiblingCoordination(sibling_id="sibling-1", claimed_targets=("port_80",)),
                SiblingCoordination(sibling_id="sibling-2", claimed_targets=("port_443",)),
            ),
        ))
    """

    model_config = {"frozen": True}

    coordinations: tuple[SiblingCoordination, ...] = ()

    @property
    def template_key(self) -> str:
        return "sibling_coordinations"

    def to_template_dict(self) -> dict[str, Any]:
        # Merge all claimed targets and discovered info
        all_claimed: list[str] = []
        all_discovered: dict[str, Any] = {}
        all_warnings: list[str] = []

        for coord in self.coordinations:
            all_claimed.extend(coord.claimed_targets)
            all_discovered.update(coord.discovered_info)
            all_warnings.extend(coord.warnings)

        return {
            "coordinations": [c.to_template_dict() for c in self.coordinations],
            "total_siblings": len(self.coordinations),
            "all_claimed_targets": list(set(all_claimed)),
            "all_discovered_info": all_discovered,
            "all_warnings": list(set(all_warnings)),
        }




