"""Domain tests for the resolved hard-predecessor ids used to scope worker context."""

from uuid import uuid4

from core.domain.aggregates.agent_session import AgentRole, AgentSession


def _config() -> dict:
    return {
        "strategy": "heuristic",
        "base": {"model": "gpt-4", "temperature": 0, "max_tokens": 100},
        "tool": "claude_code",
    }


def test_agent_stores_and_replays_hard_predecessor_ids() -> None:
    # Given: a worker created with its resolved hard-predecessor agent ids
    predecessor = uuid4()
    agent = AgentSession.create(
        agent_id=uuid4(),
        role=AgentRole.WORKER,
        config=_config(),
        parent_id=uuid4(),
        hard_predecessor_ids=[predecessor],
    )

    # Then: the ids are stored and survive replay (used to scope its context packet)
    assert agent.hard_predecessor_ids == [predecessor]
    restored = AgentSession.load_from_history(agent.events)
    assert restored.hard_predecessor_ids == [predecessor]


def test_hard_predecessor_ids_default_empty() -> None:
    # Given: a worker with no declared predecessors
    agent = AgentSession.create(
        agent_id=uuid4(), role=AgentRole.WORKER, config=_config(), parent_id=uuid4()
    )

    # Then: the field defaults empty (back-compat, no scoping)
    assert agent.hard_predecessor_ids == []
