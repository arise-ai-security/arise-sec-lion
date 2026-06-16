"""Tests for AgentOrchestrator decomposition-contract enforcement.

Core owns the enforcement ACTION: given a validator verdict it injects missing-role
leaves, drops/splits merged leaves (remapping surviving sibling-index depends_on), and
fails before spawning when the repaired set exceeds hierarchy limits. The classification
itself is a domain concern tested in the domain layer, so this uses a stub validator and
generic role labels — core stays domain-agnostic.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.application.agent_orchestrator import AgentOrchestrator
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.services.subtask_parser import AssessmentResult
from core.domain.values.agent_config import HeuristicConfig, LLMConfig
from core.domain.values.limits import HierarchyLimits
from core.domain.values.subtask import Subtask
from core.ports.decomposition_validator_port import (
    DecompositionVerdict,
    DecompositionViolation,
    SuggestedSubtask,
)


def _manager_agent(role: AgentRole = AgentRole.MANAGER) -> AgentSession:
    root_id = uuid4()
    agent = AgentSession.create(
        agent_id=uuid4(),
        parent_id=root_id,
        role=role,
        config=HeuristicConfig(
            strategy="heuristic",
            base=LLMConfig(model="m", temperature=0.5, max_tokens=4000),
        ).model_dump(),
    )
    agent.assign_task("[Phase-A] decompose this phase")
    agent.hierarchy_limits = HierarchyLimits(
        root_id=root_id,
        current_depth=1,
        max_depth=3,
        max_children_per_node=7,
        max_retries=3,
        max_total_agents=40,
        current_total_agents=2,
    )
    agent.mark_changes_as_committed()
    return agent


def _orchestrator(validator: object | None) -> AgentOrchestrator:
    child_factory = MagicMock()
    child_factory.max_total_agents = 40
    child_factory.total_created = 2
    return AgentOrchestrator(
        llm_port=AsyncMock(),
        worker_port=MagicMock(),
        prompt_builder=MagicMock(),
        child_factory=child_factory,
        decomposition_validator=validator,
    )


def _sub(description: str, depends_on: list[int] | None = None) -> Subtask:
    return Subtask(
        description=description, config={"tool": "openhands"}, depends_on=depends_on or []
    )


class _StubValidator:
    def __init__(
        self,
        verdict: DecompositionVerdict,
        hard_deps: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._verdict = verdict
        self._hard_deps = hard_deps or {}

    def classify(self, **_kwargs: object) -> DecompositionVerdict:
        return self._verdict

    def hard_dependencies(self, role_label: str) -> tuple[str, ...]:
        return self._hard_deps.get(role_label, ())


@pytest.mark.asyncio
async def test_no_validator_is_passthrough() -> None:
    orch = _orchestrator(None)
    subs = [_sub("[Role-A] do A")]
    out = await orch._enforce_decomposition_contract(_manager_agent(), subs)
    assert out == subs


@pytest.mark.asyncio
async def test_ok_verdict_is_passthrough() -> None:
    orch = _orchestrator(_StubValidator(DecompositionVerdict(ok=True)))
    subs = [_sub("[Role-A] do A")]
    out = await orch._enforce_decomposition_contract(_manager_agent(), subs)
    assert out == subs


@pytest.mark.asyncio
async def test_missing_role_is_injected_with_sibling_config() -> None:
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="missing_required_role", detail="x"),),
        additions=(SuggestedSubtask(description="[Role-B] do B"),),
    )
    orch = _orchestrator(_StubValidator(verdict))
    out = await orch._enforce_decomposition_contract(_manager_agent(), [_sub("[Role-A] do A")])
    assert out is not None
    descriptions = [s.description for s in out]
    assert "[Role-A] do A" in descriptions
    assert any(d.startswith("[Role-B]") for d in descriptions)
    # Injected leaf inherits the worker config of its siblings.
    injected = next(s for s in out if s.description.startswith("[Role-B]"))
    assert injected.config == {"tool": "openhands"}


@pytest.mark.asyncio
async def test_merged_leaf_is_dropped_and_split() -> None:
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="merged_leaf", detail="x", subtask_index=0),),
        additions=(
            SuggestedSubtask(description="[Role-A] do A"),
            SuggestedSubtask(description="[Role-B] do B"),
        ),
        removals=(0,),
    )
    orch = _orchestrator(_StubValidator(verdict))
    merged = [_sub("[Role-A] do A and [Role-B] do B")]
    out = await orch._enforce_decomposition_contract(_manager_agent(), merged)
    assert out is not None
    descriptions = [s.description for s in out]
    assert "[Role-A] do A and [Role-B] do B" not in descriptions
    assert any(d.startswith("[Role-A]") for d in descriptions)
    assert any(d.startswith("[Role-B]") for d in descriptions)


@pytest.mark.asyncio
async def test_removal_remaps_surviving_depends_on() -> None:
    # Drop leaf 0; the surviving leaf-2 depended on leaf-1 (now index 0). Its
    # depends_on must be remapped, never left pointing at a shifted/removed sibling.
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="merged_leaf", detail="x", subtask_index=0),),
        removals=(0,),
    )
    orch = _orchestrator(_StubValidator(verdict))
    subs = [
        _sub("[Role-A] merged drop", depends_on=[]),  # index 0 — removed
        _sub("[Role-B] producer", depends_on=[]),  # index 1 -> new 0
        _sub("[Role-C] consumer", depends_on=[1]),  # index 2 -> new 1, dep 1 -> 0
    ]
    out = await orch._enforce_decomposition_contract(_manager_agent(), subs)
    assert out is not None
    assert [s.description for s in out] == ["[Role-B] producer", "[Role-C] consumer"]
    consumer = next(s for s in out if s.description.startswith("[Role-C]"))
    assert consumer.depends_on == [0]  # remapped from old index 1


@pytest.mark.asyncio
async def test_injected_leaf_depends_on_its_producer() -> None:
    # Catalog hard deps (from the validator) must be resolved to corrected sibling
    # indices, so a consumer is not "ready" before its producer under DAG scheduling.
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="missing_required_role", detail="x"),),
        additions=(SuggestedSubtask(description="[Role-B] do B"),),
    )
    orch = _orchestrator(_StubValidator(verdict, hard_deps={"Role-B": ("Role-A",)}))
    out = await orch._enforce_decomposition_contract(_manager_agent(), [_sub("[Role-A] a")])
    assert out is not None
    producer_idx = next(i for i, s in enumerate(out) if s.description.startswith("[Role-A]"))
    injected = next(s for s in out if s.description.startswith("[Role-B]"))
    assert injected.depends_on == [producer_idx]


@pytest.mark.asyncio
async def test_manager_authored_leaf_gets_catalog_deps_even_when_ok() -> None:
    # Point #1: a role-complete decomposition with missing deps must still get the
    # catalog hard-dep edges enforced (verdict.ok path), not just injected leaves.
    orch = _orchestrator(
        _StubValidator(DecompositionVerdict(ok=True), hard_deps={"Role-B": ("Role-A",)})
    )
    subs = [_sub("[Role-A] a"), _sub("[Role-B] b")]  # manager set NO depends_on
    out = await orch._enforce_decomposition_contract(_manager_agent(), subs)
    assert out is not None
    consumer = next(s for s in out if s.description.startswith("[Role-B]"))
    assert consumer.depends_on == [0]  # Role-A is index 0


def test_resolve_sets_catalog_dep_to_producer_index() -> None:
    orch = _orchestrator(
        _StubValidator(DecompositionVerdict(ok=True), hard_deps={"Role-B": ("Role-A",)})
    )
    out = orch._resolve_catalog_dependencies([_sub("[Role-A] a"), _sub("[Role-B] b")])
    consumer = next(s for s in out if s.description.startswith("[Role-B]"))
    assert consumer.depends_on == [0]


def test_resolve_drops_catalog_dep_when_producer_absent() -> None:
    # The post-dedup case: the producer was removed, so the dep must drop to [] rather
    # than leave a stale/self index (the bug the post-dedup re-resolve closes).
    orch = _orchestrator(
        _StubValidator(DecompositionVerdict(ok=True), hard_deps={"Role-B": ("Role-A",)})
    )
    out = orch._resolve_catalog_dependencies([_sub("[Role-B] b", depends_on=[0])])
    assert out[0].depends_on == []


@pytest.mark.asyncio
async def test_assess_time_decompose_is_gated() -> None:
    # The combined assess+decompose path (_apply_assessment_result) must run the SAME
    # contract gate as evaluate_task — the injected role must reach _spawn_children.
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="missing_required_role", detail="x"),),
        additions=(SuggestedSubtask(description="[Role-B] do B"),),
    )
    orch = _orchestrator(_StubValidator(verdict))
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _manager_agent(role=AgentRole.PENDING)
    result = AssessmentResult(
        action="decompose", reasoning="x", subtasks=[_sub("[Role-A] a")]
    )
    await orch._apply_assessment_result(agent, result)
    orch._spawn_children.assert_awaited_once()
    spawned = orch._spawn_children.await_args.args[1]
    descriptions = [s.description for s in spawned]
    assert "[Role-A] a" in descriptions
    assert any(d.startswith("[Role-B]") for d in descriptions)


@pytest.mark.asyncio
async def test_dedup_removed_required_role_is_reinjected_post_dedup() -> None:
    # Blocker regression: dedup runs BEFORE the gate, so a required role that dedup drops
    # (e.g. a stale role registry on re-decomposition) is re-injected by the gate, not lost.
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="missing_required_role", detail="x"),),
        additions=(SuggestedSubtask(description="[Role-B] do B"),),
    )
    orch = _orchestrator(_StubValidator(verdict))
    # Simulate dedup dropping [Role-B] (cross-tree dedup against a stale registry).
    orch._dedup_subtasks_against_siblings = lambda _agent, subs: [  # type: ignore[method-assign]
        s for s in subs if not s.description.startswith("[Role-B]")
    ]
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _manager_agent()
    await orch._enforce_and_spawn(agent, [_sub("[Role-A] a"), _sub("[Role-B] b")])
    orch._spawn_children.assert_awaited_once()
    descriptions = [s.description for s in orch._spawn_children.await_args.args[1]]
    assert any(d.startswith("[Role-B]") for d in descriptions)


@pytest.mark.asyncio
async def test_repair_exceeding_child_limit_fails_before_spawning() -> None:
    # max_children_per_node=7; start at 7, inject 1 -> 8 > 7 -> unsatisfiable -> fail.
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="missing_required_role", detail="x"),),
        additions=(SuggestedSubtask(description="[Role-B] do B"),),
    )
    orch = _orchestrator(_StubValidator(verdict))
    agent = _manager_agent()
    seven = [_sub(f"[Role-{i}] x") for i in range(7)]
    out = await orch._enforce_decomposition_contract(agent, seven)
    assert out is None
    assert agent.status == AgentStatus.FAILED
