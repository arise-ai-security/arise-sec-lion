"""Validation steps for pipeline execution.

These steps validate agent state at the start of each pipeline,
ensuring the agent is in the correct role and status before proceeding.
"""

from core.application.pipeline.context import PipelineState, StepResult
from core.domain.values.enums import AgentRole, AgentStatus


class AgentRoleValidator:
    """Parameterized validator for agent role and status.

    Eliminates duplication across pipeline validators by accepting
    required roles and optional additional checks as parameters.
    """

    def __init__(
        self,
        required_roles: set[AgentRole],
        required_status: AgentStatus = AgentStatus.ANALYZING,
        require_task: bool = False,
    ) -> None:
        self._required_roles = required_roles
        self._required_status = required_status
        self._require_task = require_task

    async def execute(self, state: PipelineState) -> StepResult:
        """Validate agent state against configured requirements."""
        agent = state.agent

        if agent.role not in self._required_roles:
            roles_str = "/".join(r.name for r in self._required_roles)
            return StepResult.fail(f"Requires {roles_str} role, got {agent.role}")

        if agent.status != self._required_status:
            return StepResult.fail(
                f"Requires {self._required_status} status, got {agent.status}"
            )

        if self._require_task and not agent.task_description:
            return StepResult.fail("Requires assigned task")

        return StepResult.ok(state)


# Pre-configured validators
ValidatePendingAgent = AgentRoleValidator(
    required_roles={AgentRole.PENDING},
    require_task=True,
)

ValidateDecomposingAgent = AgentRoleValidator(
    required_roles={AgentRole.BOSS, AgentRole.MANAGER},
)

ValidateWorkerAgent = AgentRoleValidator(
    required_roles={AgentRole.WORKER},
)
