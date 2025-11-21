"""Application Layer - Data Transfer Objects (DTOs).

DTOs are simple data structures used to transfer data between layers.
They prevent domain aggregates from leaking to presentation layer.

Reference: Martin Fowler - "Patterns of Enterprise Application Architecture"
https://martinfowler.com/eaaCatalog/dataTransferObject.html

Why DTOs?
- Presentation should NOT depend on Domain aggregates
- DTOs provide a stable contract between Application and Presentation
- Domain can evolve independently without breaking Presentation
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentResultDTO:
    """DTO for presenting agent execution results.

    This DTO represents the final state of an agent after execution.
    It contains only the data needed for presentation (no business logic).

    Attributes:
        agent_id: UUID of the agent as string.
        status: Final status (COMPLETED, FAILED, etc.).
        result: Final result text (if any).
        task_description: Original task assigned to agent.
        role: Agent role (BOSS, MANAGER, WORKER).
    """

    agent_id: str
    status: str
    result: str | None
    task_description: str
    role: str


@dataclass(frozen=True)
class SystemStatisticsDTO:
    """DTO for system-wide statistics.

    Provides aggregate statistics about the multi-agent system
    without exposing internal domain details.

    Attributes:
        total_agents: Total number of agents created.
        completed: Number of agents in COMPLETED status.
        failed: Number of agents in FAILED status.
        active: Number of agents still processing.
    """

    total_agents: int
    completed: int
    failed: int
    active: int
