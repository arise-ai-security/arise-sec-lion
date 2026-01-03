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
# Thinker Justification (Design Choice 4)
# =============================================================================


class ThinkerJustification(BaseModel):
    """Thinker Justification: Context passed from thinker to worker (Design Choice 4).

    The reasoning provided by the thinker agent to explain why a specific
    subtask is assigned to a worker agent. This helps child agents understand:
    - The bigger picture (thinker's original task)
    - Their specific objective and why it's relevant
    - How to finish the subtask (suggested approach)
    - The percentage of complexity/token budget assigned
    - What deliverables are expected

    Example:
        context.add(ThinkerJustification(
            thinker_task="Build gpac environment using Dockerfile",
            objective="Establish Docker build environment with correct version",
            split_reason="Separates environment setup from build execution",
            suggested_approach="Use provided Dockerfile to clone gpac at commit...",
            why_it_works="Direct use of Dockerfile ensures consistent config",
            expected_deliverables="Docker image ready for building gpac with ASAN",
            budget_allocation="40% of budget (weight 1.6 of 4.0)",
            complexity_assessment="MODERATE: Integrating build context",
            significance="HIGH: Proper build env is critical for validation",
            resource_justification="40% recognizes importance of Docker setup",
        ))
    """

    model_config = {"frozen": True}

    # Core justification from thinker
    thinker_task: str  # The thinker's original task
    objective: str  # What this specific subtask should achieve
    split_reason: str = ""  # Why this was delegated to a sub-agent
    suggested_approach: str = ""  # Recommended execution plan
    why_it_works: str = ""  # Why the approach should succeed
    expected_deliverables: str = ""  # Concrete outputs expected

    # Budget allocation context (Design Choice 4)
    budget_allocation: str = ""  # e.g., "40% of budget (weight 1.6 of 4.0)"
    complexity_assessment: str = ""  # e.g., "MODERATE: Multi-file tracing"
    significance: str = ""  # e.g., "CRITICAL PATH: Blocks downstream"
    resource_justification: str = ""  # Why this budget level was chosen

    @property
    def template_key(self) -> str:
        return "thinker_justification"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "thinker_task": self.thinker_task,
            "objective": self.objective,
            "split_reason": self.split_reason,
            "suggested_approach": self.suggested_approach,
            "why_it_works": self.why_it_works,
            "expected_deliverables": self.expected_deliverables,
            "budget_allocation": self.budget_allocation,
            "complexity_assessment": self.complexity_assessment,
            "significance": self.significance,
            "resource_justification": self.resource_justification,
            "has_budget_context": bool(self.budget_allocation),
        }


# Backward compatibility alias
SupervisorExpectations = ThinkerJustification


# =============================================================================
# Coworker Knowledge (Design Choice 5: Worker Report Context Passing)
# =============================================================================


class CoworkerKnowledgeEntry(BaseModel):
    """Single entry of knowledge published by an earlier coworker.

    Represents curated knowledge from a completed worker that can help
    subsequent workers avoid redundant work. Published to global shared
    context, accessible by any worker in the execution hierarchy.
    """

    model_config = {"frozen": True}

    key: str  # Unique identifier (usually the task objective)
    objective: str  # What the coworker was asked to do
    relevance: str  # Why this is relevant to current worker
    key_findings: tuple[str, ...] = ()  # Important discoveries
    deliverables: tuple[str, ...] = ()  # Artifacts produced
    source_worker_id: str = ""  # Worker who discovered this
    published_by: str = ""  # Thinker who approved and published

    approach: str = ""  # How the task was executed
    reasoning: str = ""  # Worker's reasoning for chosen approach
    work_analysis: str = ""  # Analysis of work performed
    challenges: str = ""  # Difficulties faced during execution
    observations: str = ""  # What was discovered during execution
    fulfillment_evidence: str = ""  # How expectations were met


class CoworkerKnowledge(BaseModel):
    """Published knowledge from earlier coworkers (Design Choice 5).

    Use to share runtime discoveries across all workers in the execution,
    enabling online learning and preventing redundant work. This is stored
    in the global SharedExecutionContext, making it accessible to any
    worker regardless of their position in the agent tree.

    Example:
        context.add(CoworkerKnowledge(entries=(
            CoworkerKnowledgeEntry(
                key="Map vulnerable code path",
                objective="Map out the MP4 box structure leading to vulnerability",
                relevance="Provides exact file location and trigger conditions",
                key_findings=(
                    "Vulnerable line: drm_sample.c:1562",
                    "Required boxes: moov → trak → mdia → minf → stbl",
                    "Trigger: IV_size == 0 with missing aux_info_offset",
                ),
                deliverables=("path_map.md",),
                source_worker_id="worker-e286dfa6",
                published_by="manager-123",
            ),
        )))
    """

    model_config = {"frozen": True}

    entries: tuple[CoworkerKnowledgeEntry, ...] = ()

    @property
    def template_key(self) -> str:
        return "coworker_knowledge"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "entries": [e.model_dump() for e in self.entries],
            "count": len(self.entries),
        }




