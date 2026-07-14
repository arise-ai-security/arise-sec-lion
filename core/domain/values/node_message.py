"""NodeMessage — unified inter-agent communication.

A single discriminated-union type for all data that flows between tree nodes:
- Briefing  (down)    Parent → Child at spawn time
- Report    (up)      Child → Parent on completion
- Handoff   (lateral) Sibling ↔ Sibling during sequential execution
"""

from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, Discriminator, Tag, computed_field


if TYPE_CHECKING:
    from core.domain.aggregates.agent_session import AgentSession


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


class Ancestor(BaseModel):
    """Lightweight ancestor entry for lineage chain."""

    model_config = {"frozen": True}

    agent_id: str
    role: str
    task_summary: str  # First 300 chars of task description

    @classmethod
    def from_agent(cls, agent: "AgentSession") -> Self:
        task_summary = (agent.task_description or "")[:300]
        return cls(
            agent_id=str(agent.agent_id),
            role=agent.role.value,
            task_summary=task_summary,
        )


# Separates the worker's tool/command transcript from its closing message in
# ``agent.result``. Producers (worker adapters) append it before the finish
# message; consumers prefer the text after it as the sibling-facing summary —
# raw transcript tails carry failed tool calls and path noise siblings can't use.
WORKER_CONCLUSION_MARKER = "--- Agent Conclusion ---"


class PeerStatus(BaseModel):
    """Status snapshot of a sibling worker."""

    model_config = {"frozen": True}

    agent_id: str
    sibling_index: int
    status: str  # pending, analyzing, in_progress, completed, failed
    task_summary: str
    result_summary: str | None = None


class SharedDecision(BaseModel):
    """Decision visible across sibling workers."""

    model_config = {"frozen": True}

    key: str
    value: str
    rationale: str = ""
    decided_by: str = ""


# ---------------------------------------------------------------------------
# NodeMessage variants
# ---------------------------------------------------------------------------


class Briefing(BaseModel):
    """Parent → Child: context passed at spawn time."""

    model_config = {"frozen": True}

    direction: Literal["down"] = "down"
    parent_task: str
    parent_role: str
    ancestry: tuple[Ancestor, ...] = ()
    decisions: tuple[str, ...] = ()
    # Per-child justification from parent's decomposition (Design Choice 4).
    # Contains keys like "objective", "plan", and domain-specific insights.
    subtask_justification: dict[str, str] = {}
    evidence_references: tuple[str, ...] = ()

    @classmethod
    def simple(cls, parent_task: str, parent_role: str = "boss") -> Self:
        return cls(parent_task=parent_task, parent_role=parent_role)


class Report(BaseModel):
    """Child → Parent: structured result on completion."""

    model_config = {"frozen": True}

    direction: Literal["up"] = "up"
    task: str = ""
    result: str
    artifacts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    execution_summary: dict[str, Any] = {}

    @classmethod
    def simple(cls, result: str, task: str = "") -> Self:
        return cls(result=result, task=task)


class Handoff(BaseModel):
    """Sibling ↔ Sibling: peer coordination snapshot."""

    model_config = {"frozen": True}

    direction: Literal["lateral"] = "lateral"
    parent_task: str | None = None
    current_sibling_index: int | None = None
    siblings: tuple[PeerStatus, ...] = ()
    shared_decisions: tuple[SharedDecision, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_siblings(self) -> int:
        return len(self.siblings) + (1 if self.current_sibling_index is not None else 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def completed_count(self) -> int:
        return sum(1 for s in self.siblings if s.status == "completed")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_progress_count(self) -> int:
        return sum(1 for s in self.siblings if s.status in ("analyzing", "in_progress"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_downstream_siblings(self) -> bool:
        """Whether any peer executes after the current worker."""
        if self.current_sibling_index is None:
            return False
        return any(s.sibling_index > self.current_sibling_index for s in self.siblings)

    def to_template_dict(self) -> dict[str, Any]:
        return self.model_dump()


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------


def _direction_discriminator(v: Any) -> str:
    if isinstance(v, dict):
        return v.get("direction", "down")
    return getattr(v, "direction", "down")


NodeMessage = Annotated[
    Annotated[Briefing, Tag("down")]
    | Annotated[Report, Tag("up")]
    | Annotated[Handoff, Tag("lateral")],
    Discriminator(_direction_discriminator),
]
"""Union type for all inter-node messages. Discriminated on ``direction``."""


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_briefing(
    agent: "AgentSession",
    parent_briefing: Briefing | None = None,
) -> Briefing:
    """Build a Briefing from the parent agent for passing to children.

    Appends the current agent to the ancestry chain.
    """
    if parent_briefing is not None:
        ancestry = list(parent_briefing.ancestry)
    else:
        ancestry = []
    ancestry.append(Ancestor.from_agent(agent))

    return Briefing(
        parent_task=agent.task_description or "",
        parent_role=agent.role.value,
        ancestry=tuple(ancestry),
        decisions=tuple(agent.local_decisions),
    )
