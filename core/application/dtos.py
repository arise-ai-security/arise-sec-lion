"""DTOs for transferring data between Application and Presentation layers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentResultDTO:
    """Agent execution result for presentation layer."""

    agent_id: str
    status: str
    result: str | None
    task_description: str
    role: str


@dataclass(frozen=True)
class SystemStatisticsDTO:
    """System-wide agent statistics."""

    total_agents: int
    completed: int
    failed: int
    active: int
