"""Domain enums for the multi-agent system.

Separated into own module to avoid circular imports between model.py and prompt_builder.py.
"""

from enum import Enum


class AgentRole(str, Enum):
    """Agent role: BOSS (root), PENDING (awaiting eval), MANAGER (decomposes), WORKER (executes)."""

    BOSS = "boss"
    PENDING = "pending"
    MANAGER = "manager"
    WORKER = "worker"


class AgentStatus(str, Enum):
    """Agent execution status."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
