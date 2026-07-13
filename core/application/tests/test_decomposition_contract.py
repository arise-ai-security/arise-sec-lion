"""Tests for AgentOrchestrator decomposition-contract enforcement.

Core owns the enforcement ACTION: given a validator verdict it injects missing-role
leaves, drops/splits merged leaves (remapping surviving sibling-index depends_on), and
fails before spawning when the repaired set exceeds hierarchy limits. The classification
itself is a domain concern tested in the domain layer, so this uses a stub validator and
generic role labels — core stays domain-agnostic.
"""

import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from core.application.agent_orchestrator import AgentOrchestrator
from core.application.services.lifecycle.child_factory import ChildAgentFactory
from core.application.services.lifecycle.hierarchy_limits_registry import (
    HierarchyLimitsRegistry,
)
from core.domain.aggregates.agent_session import AgentRole, AgentSession, AgentStatus
from core.domain.services.subtask_parser import AssessmentResult
from core.domain.values.agent_config import HeuristicConfig, LLMConfig
from core.domain.values.failure import ChildFailureRecord
from core.domain.values.limits import HierarchyLimits
from core.domain.values.llm_response import LLMResponse, LLMUsage
from core.domain.values.subtask import Subtask
from core.ports.decomposition_policy_port import InitialDecomposition, InitialSubtask
from core.ports.decomposition_validator_port import (
    DecompositionVerdict,
    DecompositionViolation,
    SuggestedSubtask,
    ValidatedSubtask,
)


def _manager_agent(
    role: AgentRole = AgentRole.MANAGER,
    task_description: str = "[Phase-A] decompose this phase",
) -> AgentSession:
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
    agent.assign_task(task_description)
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


def _root_agent() -> AgentSession:
    root_id = uuid4()
    agent = AgentSession.create(
        agent_id=root_id,
        parent_id=None,
        role=AgentRole.BOSS,
        config=HeuristicConfig(
            strategy="heuristic",
            base=LLMConfig(model="m", temperature=0.5, max_tokens=4000),
        ).model_dump(),
    )
    agent.assign_task("Coordinate the work")
    agent.hierarchy_limits = HierarchyLimits(
        root_id=root_id,
        current_depth=0,
        max_depth=3,
        max_children_per_node=7,
        max_retries=3,
        max_total_agents=40,
        current_total_agents=1,
    )
    agent.mark_changes_as_committed()
    return agent


def _orchestrator(
    validator: object | None,
    *,
    policy: object | None = None,
    include_manager_layer: bool = True,
    max_redecompositions: int = 0,
    child_factory: ChildAgentFactory | None = None,
) -> AgentOrchestrator:
    if child_factory is None:
        child_factory = MagicMock()
        child_factory.max_total_agents = 40
        child_factory.total_created = 2
        child_factory.get_sibling_role_prefixes = MagicMock(return_value=set())
        child_factory.get_tree_role_prefixes = MagicMock(return_value=set())
        child_factory.get_completed_role_prefixes = MagicMock(return_value=set())
        child_factory.get_failed_role_prefixes = MagicMock(return_value=set())
    return AgentOrchestrator(
        llm_port=AsyncMock(),
        worker_port=MagicMock(),
        prompt_builder=MagicMock(),
        child_factory=child_factory,
        decomposition_policy=policy,
        decomposition_validator=validator,
        include_manager_layer=include_manager_layer,
        max_redecompositions=max_redecompositions,
    )


async def _enforce(orch: AgentOrchestrator, agent, subs):
    corrected, _route = await orch._enforce_decomposition_contract(agent, subs)
    return corrected


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


class _StubInitialPolicy:
    def __init__(
        self,
        *,
        root: InitialDecomposition | None = None,
        phases: dict[str, InitialDecomposition] | None = None,
    ) -> None:
        self._root = root
        self._phases = phases or {}
        self.calls: list[tuple[str, bool, int]] = []

    def select_initial(
        self,
        *,
        parent_task_description: str,
        domain_context: object | None,
        is_root: bool,
        redecomposition_count: int,
    ) -> InitialDecomposition | None:
        self.calls.append((parent_task_description, is_root, redecomposition_count))
        if redecomposition_count > 0:
            return None
        if is_root:
            return self._root
        return self._phases.get(parent_task_description)


@pytest.mark.asyncio
async def test_root_initial_policy_bypasses_llm_and_preserves_dependencies() -> None:
    plan = InitialDecomposition(
        subtasks=(
            InitialSubtask(
                description="[Phase-A] establish the shared input",
                estimated_complexity="complex",
            ),
            InitialSubtask(
                description="[Phase-B] consume the shared input",
                estimated_complexity="complex",
                depends_on=(0,),
            ),
        ),
        policy_version="initial-policy-v1",
        phase="skeleton",
        triggers=("run_started",),
        evidence_references=("plan:root",),
    )
    policy = _StubInitialPolicy(root=plan)
    orch = _orchestrator(None, policy=policy, max_redecompositions=2)
    orch._query_llm = AsyncMock()  # type: ignore[method-assign]
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _root_agent()

    await orch.evaluate_task(agent)

    orch._query_llm.assert_not_awaited()
    orch._spawn_children.assert_awaited_once()
    spawned = orch._spawn_children.await_args.args[1]
    assert [subtask.description for subtask in spawned] == [
        "[Phase-A] establish the shared input",
        "[Phase-B] consume the shared input",
    ]
    assert [subtask.depends_on for subtask in spawned] == [[], [0]]
    assert policy.calls == [("Coordinate the work", True, 0)]
    assert not any(event.__class__.__name__ == "PromptSent" for event in agent.events)

    route = next(
        event for event in agent.events if event.__class__.__name__ == "PhaseRouteSelected"
    )
    assert route.policy_version == "initial-policy-v1"
    assert route.phase == "skeleton"
    assert route.selected_roles == ["Phase-A", "Phase-B"]
    assert route.task_instructions == {
        "Phase-A": "establish the shared input",
        "Phase-B": "consume the shared input",
    }
    assert route.task_sources == {"Phase-A": "host_policy", "Phase-B": "host_policy"}
    assert route.evidence_references == ["plan:root"]
    assert route.remaining_budget == 2


@pytest.mark.asyncio
async def test_phase_initial_policy_bypasses_assessment_llm_and_spawns_compact_route() -> None:
    task = "[Phase-A] execute the phase gate"
    plan = InitialDecomposition(
        subtasks=(
            InitialSubtask(description="[Role-A] produce evidence"),
            InitialSubtask(
                description="[Role-B] validate evidence",
                depends_on=(0,),
            ),
        ),
        policy_version="initial-policy-v1",
        phase="Phase-A",
        triggers=("phase_started",),
    )
    policy = _StubInitialPolicy(phases={task: plan})
    orch = _orchestrator(None, policy=policy, max_redecompositions=1)
    orch._query_llm = AsyncMock()  # type: ignore[method-assign]
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _manager_agent(role=AgentRole.PENDING, task_description=task)

    await orch.assess_task(agent)

    orch._query_llm.assert_not_awaited()
    orch._spawn_children.assert_awaited_once()
    assert agent.role == AgentRole.MANAGER
    spawned = orch._spawn_children.await_args.args[1]
    assert [subtask.description for subtask in spawned] == [
        "[Role-A] produce evidence",
        "[Role-B] validate evidence",
    ]
    assert [subtask.depends_on for subtask in spawned] == [[], [0]]
    assert policy.calls == [(task, False, 0)]
    assert not any(event.__class__.__name__ == "PromptSent" for event in agent.events)

    route = next(
        event for event in agent.events if event.__class__.__name__ == "PhaseRouteSelected"
    )
    assert route.route == "compact"
    assert route.selected_roles == ["Role-A", "Role-B"]
    assert route.task_instructions["Role-B"] == "validate evidence"
    assert route.task_sources == {"Role-A": "host_policy", "Role-B": "host_policy"}
    assert route.remaining_budget == 1


@pytest.mark.asyncio
async def test_grouping_role_does_not_relabel_same_named_host_leaf_as_repair() -> None:
    # Given: A grouping agent and its Host-selected leaf intentionally share a role label
    root_id = uuid4()
    manager_id = uuid4()
    registry = HierarchyLimitsRegistry()
    registry.create_root(
        root_id,
        max_depth=3,
        max_children_per_node=7,
        max_retries=3,
        max_total_agents=40,
    )
    manager_limits = registry.propagate_to_child(root_id, manager_id)
    assert manager_limits is not None
    registry.register_role(manager_id, "Role-X", parent_id=root_id)
    child_factory = ChildAgentFactory(
        repository=AsyncMock(),
        limits_registry=registry,
        max_total_agents=40,
    )
    task = "[Role-X] coordinate the outcome"
    plan = InitialDecomposition(
        subtasks=(InitialSubtask(description="[Role-X] produce the outcome"),),
        policy_version="initial-policy-v1",
        phase="Role-X",
        triggers=("phase_started",),
    )
    orchestrator = _orchestrator(
        None,
        policy=_StubInitialPolicy(phases={task: plan}),
        child_factory=child_factory,
    )
    orchestrator._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    manager = AgentSession.create(
        agent_id=manager_id,
        parent_id=root_id,
        role=AgentRole.PENDING,
        config=HeuristicConfig(
            strategy="heuristic",
            base=LLMConfig(model="m", temperature=0.5, max_tokens=4000),
        ).model_dump(),
    )
    manager.assign_task(task)
    manager.set_hierarchy_limits(manager_limits)

    # When: The grouping agent applies its deterministic initial route
    await orchestrator.assess_task(manager)

    # Then: Its own label does not deduplicate the leaf or change Host provenance
    orchestrator._spawn_children.assert_awaited_once()
    [spawned] = orchestrator._spawn_children.await_args.args[1]
    assert spawned.description == "[Role-X] produce the outcome"
    route = next(
        event for event in manager.events if event.__class__.__name__ == "PhaseRouteSelected"
    )
    assert route.task_sources == {"Role-X": "host_policy"}

    # And: A different same-labelled agent still blocks a duplicate leaf
    sibling_id = uuid4()
    registry.propagate_to_child(root_id, sibling_id)
    registry.register_role(sibling_id, "Role-X", parent_id=root_id)
    assert orchestrator._dedup_subtasks_against_siblings(
        manager, [_sub("[Role-X] duplicate outcome")]
    ) == []


@pytest.mark.asyncio
async def test_phase_redecomposition_uses_existing_manager_llm_path_once() -> None:
    policy = _StubInitialPolicy()
    orch = _orchestrator(None, policy=policy, max_redecompositions=1)
    orch._prompt_builder.build_manager_decomposition_prompt.return_value = "manager recovery"
    response = LLMResponse(
        content=json.dumps(
            [
                {
                    "description": "[Role-A] retry using the recorded failure",
                    "config": HeuristicConfig(
                        strategy="heuristic",
                        base=LLMConfig(model="m", temperature=0.5, max_tokens=4000),
                    ).model_dump(),
                    "evidence_references": ["child_failed:123"],
                }
            ]
        ),
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="m",
        cost_usd=0,
    )
    orch._query_llm = AsyncMock(return_value=response)  # type: ignore[method-assign]
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _manager_agent()
    agent.redecomposition_count = 1
    failure = ChildFailureRecord(
        child_id=uuid4(),
        child_task="[Role-A] first attempt",
        reason="required evidence missing",
        digest="FAILURE: artifact absent",
    )
    agent.failure_history = [failure]

    await orch.evaluate_task(agent)

    assert policy.calls == [("[Phase-A] decompose this phase", False, 1)]
    orch._query_llm.assert_awaited_once()
    prompt_call = orch._prompt_builder.build_manager_decomposition_prompt.call_args
    assert prompt_call.kwargs["failure_history"] == (failure,)
    orch._spawn_children.assert_awaited_once()
    spawned = orch._spawn_children.await_args.args[1]
    assert spawned[0].description == "[Role-A] retry using the recorded failure"
    assert spawned[0].evidence_references == ("child_failed:123",)


@pytest.mark.asyncio
async def test_non_manager_redecomposition_fails_without_llm_routing() -> None:
    policy = _StubInitialPolicy()
    orch = _orchestrator(None, policy=policy, max_redecompositions=1)
    orch._query_llm = AsyncMock()  # type: ignore[method-assign]
    agent = _root_agent()
    agent.redecomposition_count = 1

    await orch.evaluate_task(agent)

    assert agent.status == AgentStatus.FAILED
    orch._query_llm.assert_not_awaited()
    assert policy.calls == []


def test_manager_layer_flattening_preserves_nested_and_cross_group_dependencies() -> None:
    root_plan = InitialDecomposition(
        subtasks=(
            InitialSubtask(
                description="[Phase-A] first group",
                estimated_complexity="complex",
            ),
            InitialSubtask(
                description="[Phase-B] second group",
                estimated_complexity="complex",
                depends_on=(0,),
            ),
        ),
        policy_version="initial-policy-v1",
        phase="skeleton",
        triggers=("run_started",),
    )
    phase_a = InitialDecomposition(
        subtasks=(
            InitialSubtask(description="[Role-X1] start X"),
            InitialSubtask(description="[Role-X2] finish X", depends_on=(0,)),
        ),
        policy_version="initial-policy-v1",
        phase="Phase-A",
    )
    phase_b = InitialDecomposition(
        subtasks=(
            InitialSubtask(description="[Role-B1] start B"),
            InitialSubtask(description="[Role-B2] finish B", depends_on=(0,)),
        ),
        policy_version="initial-policy-v1",
        phase="Phase-B",
    )
    policy = _StubInitialPolicy(
        root=root_plan,
        phases={
            "[Phase-A] first group": phase_a,
            "[Phase-B] second group": phase_b,
        },
    )
    orch = _orchestrator(
        None,
        policy=policy,
        include_manager_layer=False,
        max_redecompositions=1,
    )

    initial = orch._contract.initial_decomposition(_root_agent())

    assert initial is not None
    subtasks, route = initial
    assert [subtask.description for subtask in subtasks] == [
        "[Role-X1] start X",
        "[Role-X2] finish X",
        "[Role-B1] start B",
        "[Role-B2] finish B",
    ]
    assert [subtask.depends_on for subtask in subtasks] == [[], [0], [1], [2]]
    assert route.triggers == ("run_started", "manager_layer_omitted")
    assert route.remaining_budget == 1


@pytest.mark.asyncio
async def test_initial_route_provenance_uses_post_validation_task_instructions() -> None:
    plan = InitialDecomposition(
        subtasks=(InitialSubtask(description="[Role-A] stale instruction"),),
        policy_version="initial-policy-v1",
        phase="Phase-A",
        triggers=("phase_started",),
    )
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="invalid_role", detail="replace A"),),
        additions=(SuggestedSubtask(description="[Role-B] corrected instruction"),),
        removals=(0,),
    )
    orch = _orchestrator(_StubValidator(verdict), policy=_StubInitialPolicy(root=plan))
    orch._query_llm = AsyncMock()  # type: ignore[method-assign]
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _root_agent()

    await orch.evaluate_task(agent)

    spawned = orch._spawn_children.await_args.args[1]
    assert [subtask.description for subtask in spawned] == [
        "[Role-B] corrected instruction"
    ]
    route = next(
        event for event in agent.events if event.__class__.__name__ == "PhaseRouteSelected"
    )
    assert route.selected_roles == ["Role-B"]
    assert route.task_instructions == {"Role-B": "corrected instruction"}
    assert route.task_sources == {"Role-B": "host_repair"}
    assert "Role-A" not in route.task_instructions


@pytest.mark.asyncio
async def test_no_validator_is_passthrough() -> None:
    orch = _orchestrator(None)
    subs = [_sub("[Role-A] do A")]
    out = await _enforce(orch, _manager_agent(), subs)
    assert out == subs


@pytest.mark.asyncio
async def test_ok_verdict_is_passthrough() -> None:
    orch = _orchestrator(_StubValidator(DecompositionVerdict(ok=True)))
    subs = [_sub("[Role-A] do A")]
    out = await _enforce(orch, _manager_agent(), subs)
    assert out == subs


@pytest.mark.asyncio
async def test_missing_role_is_injected_with_sibling_config() -> None:
    verdict = DecompositionVerdict(
        ok=False,
        violations=(DecompositionViolation(kind="missing_required_role", detail="x"),),
        additions=(SuggestedSubtask(description="[Role-B] do B"),),
    )
    orch = _orchestrator(_StubValidator(verdict))
    out = await _enforce(orch, _manager_agent(), [_sub("[Role-A] do A")])
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
    out = await _enforce(orch, _manager_agent(), merged)
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
    out = await _enforce(orch, _manager_agent(), subs)
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
    out = await _enforce(orch, _manager_agent(), [_sub("[Role-A] a")])
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
    out = await _enforce(orch, _manager_agent(), subs)
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
    out = await _enforce(orch, agent, seven)
    assert out is None
    assert agent.status == AgentStatus.FAILED


@pytest.mark.asyncio
async def test_redecomposition_allows_failed_and_completed_role_reissue() -> None:
    orch = _orchestrator(None)
    factory = orch._child_factory
    factory.get_tree_role_prefixes = MagicMock(return_value={"Role-A", "Role-B", "Role-C"})
    factory.get_completed_role_prefixes = MagicMock(return_value={"Role-A"})
    factory.get_failed_role_prefixes = MagicMock(return_value={"Role-B"})
    factory.get_sibling_role_prefixes = MagicMock(return_value=set())
    agent = _manager_agent()
    agent.redecomposition_count = 1
    agent.failure_history = [
        ChildFailureRecord(
            child_id=uuid4(),
            child_task="[Role-B] failed work",
            reason="crash",
        )
    ]
    kept = orch._dedup_subtasks_against_siblings(
        agent,
        [
            _sub("[Role-A] completed owner — may reissue"),
            _sub("[Role-B] failed — may reissue"),
            _sub("[Role-C] other used — strip"),
            _sub("[Role-D] fresh — keep"),
        ],
    )
    labels = [s.description.split("]")[0] for s in kept]
    assert "[Role-A" in labels
    assert "[Role-B" in labels
    assert "[Role-C" not in labels
    assert "[Role-D" in labels


def test_stale_completion_does_not_override_replacement_generation() -> None:
    # Given: A completed role generation
    root_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    registry = HierarchyLimitsRegistry()
    registry.create_root(
        root_id,
        max_depth=3,
        max_children_per_node=7,
        max_retries=3,
        max_total_agents=40,
    )
    registry.propagate_to_child(root_id, first_id)
    registry.register_role(first_id, "Role-A", parent_id=root_id)
    registry.mark_role_completed(first_id)
    assert registry.get_completed_role_prefixes(first_id) == {"Role-A"}

    # When: A case-variant replacement is registered and the old agent completes again
    registry.propagate_to_child(root_id, second_id)
    registry.register_role(second_id, "role-a", parent_id=root_id)
    registry.mark_role_completed(first_id)

    # Then: Registration clears the old outcome and the stale completion is ignored
    assert registry.get_completed_role_prefixes(second_id) == set()
    assert registry.get_failed_role_prefixes(second_id) == set()


def test_stale_failure_does_not_override_replacement_generation() -> None:
    # Given: A failed role generation
    root_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    registry = HierarchyLimitsRegistry()
    registry.create_root(
        root_id,
        max_depth=3,
        max_children_per_node=7,
        max_retries=3,
        max_total_agents=40,
    )
    registry.propagate_to_child(root_id, first_id)
    registry.register_role(first_id, "Role-A", parent_id=root_id)
    registry.mark_role_failed(first_id)
    assert registry.get_failed_role_prefixes(first_id) == {"Role-A"}

    # When: A case-variant replacement is registered and the old agent fails again
    registry.propagate_to_child(root_id, second_id)
    registry.register_role(second_id, "ROLE-A", parent_id=root_id)
    registry.mark_role_failed(first_id)

    # Then: Registration clears the old outcome and the stale failure is ignored
    assert registry.get_completed_role_prefixes(second_id) == set()
    assert registry.get_failed_role_prefixes(second_id) == set()


def test_current_generation_completion_clears_failure() -> None:
    # Given: The current role generation has failed
    root_id = uuid4()
    agent_id = uuid4()
    registry = HierarchyLimitsRegistry()
    registry.create_root(
        root_id,
        max_depth=3,
        max_children_per_node=7,
        max_retries=3,
        max_total_agents=40,
    )
    registry.propagate_to_child(root_id, agent_id)
    registry.register_role(agent_id, "Role-A", parent_id=root_id)
    registry.mark_role_failed(agent_id)

    # When: That same generation later completes
    registry.mark_role_completed(agent_id)

    # Then: Completion replaces the failed outcome
    assert registry.get_completed_role_prefixes(agent_id) == {"Role-A"}
    assert registry.get_failed_role_prefixes(agent_id) == set()


def test_dedup_allows_failed_leaf_with_same_role_as_manager() -> None:
    orch = _orchestrator(None)
    factory = orch._child_factory
    factory.get_tree_role_prefixes = MagicMock(return_value={"Role-X"})
    factory.get_completed_role_prefixes = MagicMock(return_value=set())
    factory.get_failed_role_prefixes = MagicMock(return_value={"Role-X"})
    factory.get_sibling_role_prefixes = MagicMock(return_value=set())
    agent = _manager_agent(task_description="[Role-X] coordinate the outcome")
    agent.failure_history = [
        ChildFailureRecord(
            child_id=uuid4(),
            child_task="[Role-X] failed leaf",
            reason="missing evidence",
        )
    ]

    kept = orch._dedup_subtasks_against_siblings(
        agent,
        [_sub("[Role-X] retry with failure-specific instructions")],
    )

    assert [subtask.description for subtask in kept] == [
        "[Role-X] retry with failure-specific instructions"
    ]


@pytest.mark.asyncio
async def test_manager_route_provenance_recorded_after_validation() -> None:
    verdict = DecompositionVerdict(
        ok=True,
        route="escalated",
        triggers=("phase_gate_failed", "manager_llm"),
        policy_version="policy-v2",
        phase="Phase-A",
        remaining_budget=0,
    )
    orch = _orchestrator(_StubValidator(verdict))
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _manager_agent()
    await orch._enforce_and_spawn(agent, [_sub("[Role-A] a")])
    route_events = [
        e for e in agent.events if e.__class__.__name__ == "PhaseRouteSelected"
    ]
    assert len(route_events) == 1
    assert route_events[0].route == "escalated"
    assert route_events[0].triggers == ["phase_gate_failed", "manager_llm"]
    assert any("Role-A" in r for r in route_events[0].selected_roles)
    assert route_events[0].task_sources == {"Role-A": "llm"}


@pytest.mark.asyncio
async def test_adaptive_route_provenance_distinguishes_llm_and_host_repair() -> None:
    verdict = DecompositionVerdict(
        ok=False,
        violations=(
            DecompositionViolation(kind="missing_hard_dependency", detail="Role-B"),
        ),
        additions=(SuggestedSubtask(description="[Role-B] trusted fallback instruction"),),
        route="escalated",
        triggers=("phase_gate_failed", "manager_llm", "host_dependency_closure"),
        policy_version="policy-v2",
        phase="Phase-A",
        evidence_references=("child_failed:123",),
    )
    orch = _orchestrator(_StubValidator(verdict))
    orch._spawn_children = AsyncMock(return_value=None)  # type: ignore[method-assign]
    agent = _manager_agent()
    manager_subtask = Subtask(
        description="[Role-A] revise using failure.log",
        config={"tool": "openhands"},
        evidence_references=("artifact:failure.log",),
        selection_source="host_policy",
    )

    await orch._enforce_and_spawn(agent, [manager_subtask])

    route = next(
        event for event in agent.events if event.__class__.__name__ == "PhaseRouteSelected"
    )
    assert route.selected_roles == ["Role-A", "Role-B"]
    assert route.task_instructions == {
        "Role-A": "revise using failure.log",
        "Role-B": "trusted fallback instruction",
    }
    assert route.task_sources == {"Role-A": "llm", "Role-B": "host_repair"}
    assert route.evidence_references == ["child_failed:123", "artifact:failure.log"]
    assert route.triggers == [
        "phase_gate_failed",
        "manager_llm",
        "host_dependency_closure",
    ]
    spawned = orch._spawn_children.await_args.args[1]
    repaired = next(subtask for subtask in spawned if subtask.description.startswith("[Role-B]"))
    assert repaired.evidence_references == ("child_failed:123",)


@pytest.mark.asyncio
async def test_validated_leaf_preserves_manager_content_but_host_binds_structure() -> None:
    verdict = DecompositionVerdict(
        ok=True,
        route="escalated",
        policy_version="policy-v2",
        phase="Phase-A",
        validated_subtasks=(
            ValidatedSubtask(
                subtask_index=0,
                canonical_description="[Role-A] use the recorded failure",
            ),
        ),
    )
    orch = _orchestrator(_StubValidator(verdict))
    agent = _manager_agent()
    failed_child_id = uuid4()
    agent.failure_history = [
        ChildFailureRecord(
            child_id=failed_child_id,
            child_task="[Role-A] failed leaf",
            reason="missing evidence",
        )
    ]
    manager_subtask = Subtask(
        description="[role-a] use the recorded failure",
        config={"tool": "untrusted"},
        estimated_complexity="complex",
        criticality="optional",
        dependency_failure_policy="continue",
        execution_mode="procedural",
        procedure_ref="parent_selected",
        procedure_params={"unsafe": True},
        justification={"plan": "preserve this"},
        success_criteria="preserve this criterion",
    )

    corrected = await _enforce(orch, agent, [manager_subtask])

    assert corrected is not None
    [leaf] = corrected
    assert leaf.description == "[Role-A] use the recorded failure"
    assert leaf.config == agent.config.model_dump()
    assert leaf.estimated_complexity == "simple"
    assert leaf.criticality == "required"
    assert leaf.dependency_type == "finish_to_start"
    assert leaf.dependency_failure_policy == "block"
    assert leaf.execution_mode == "auto"
    assert leaf.procedure_ref == ""
    assert leaf.procedure_params == {}
    assert leaf.evidence_references == (f"child_failed:{failed_child_id}",)
    assert leaf.justification == {"plan": "preserve this"}
    assert leaf.success_criteria == "preserve this criterion"
