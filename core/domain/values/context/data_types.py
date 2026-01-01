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
# JSON Global Context (System-Wide JSON Data)
# =============================================================================


class JsonGlobalData(BaseModel):
    """JSON-formatted global data available to all agents.

    Use for structured configuration, target information, or any JSON
    data that every node in the hierarchy should have access to.

    The data is rendered as structured XML in prompts, with support
    for nested objects, arrays, and primitive types.

    HOW TO ADD:
    1. Create JsonGlobalData with your JSON structure
    2. Add to ContextComposer in a pipeline step
    3. Template renders automatically via json_global.j2

    Example:
        # Define structured JSON data
        json_data = JsonGlobalData(
            label="target_system",
            data={
                "host": "192.168.1.100",
                "ports": [80, 443, 8080],
                "services": {
                    "web": {"port": 80, "technology": "nginx"},
                    "api": {"port": 8080, "version": "v2"},
                },
                "credentials": None,  # Unknown
            },
            schema_hint="Target system configuration for penetration testing",
        )
        context.add(json_data)

        # In template (rendered automatically):
        # <JSON_DATA label="target_system" hint="Target system...">
        # <host>192.168.1.100</host>
        # <ports>[80, 443, 8080]</ports>
        # <services>
        #   <web>{"port": 80, "technology": "nginx"}</web>
        #   <api>{"port": 8080, "version": "v2"}</api>
        # </services>
        # </JSON_DATA>
    """

    model_config = {"frozen": True}

    label: str  # Identifier for this data (e.g., "target_system", "scan_config")
    data: dict[str, Any]  # The JSON data
    schema_hint: str = ""  # Optional description of the data structure

    @property
    def template_key(self) -> str:
        return f"json_global_{self.label}"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "schema_hint": self.schema_hint,
            "data": self.data,
        }


class JsonGlobalDataCollection(BaseModel):
    """Collection of multiple JSON global data blocks.

    Use when you need to pass multiple distinct JSON data structures
    to all agents.

    Example:
        context.add(JsonGlobalDataCollection(
            items=(
                JsonGlobalData(label="target", data={...}),
                JsonGlobalData(label="config", data={...}),
            ),
        ))
    """

    model_config = {"frozen": True}

    items: tuple[JsonGlobalData, ...] = ()

    @property
    def template_key(self) -> str:
        return "json_global_collection"

    def to_template_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_template_dict() for item in self.items],
            "total_count": len(self.items),
            "labels": [item.label for item in self.items],
        }
