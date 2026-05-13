"""Domain service for resolving operation-specific LLM configurations."""

from typing import Literal

from core.domain.values.agent_config import AgentConfig, LLMConfig


type OperationType = Literal["complexity_evaluation", "task_decomposition", "task_assessment"]


class ConfigResolver:
    """Resolves AgentConfig to LLMConfig for specific operations.

    Applies domain heuristics:
    - complexity_evaluation: Lower temperature (0.3), capped tokens (500)
    - task_decomposition: Uses base config as-is
    """

    @staticmethod
    def resolve(config: AgentConfig, operation: OperationType) -> LLMConfig:
        """Resolve config for specific operation using heuristic rules."""
        base = config.base

        if operation == "complexity_evaluation":
            # Heuristic overrides are hardcoded (temperature=0.3, max_tokens=500
            # cap). Future: source these per-operation overrides from
            # ``Settings`` if deployments need to tune them without a code edit.
            return LLMConfig(
                model=base.model,
                temperature=0.3,
                max_tokens=min(base.max_tokens, 500),
                api_base=base.api_base,
                api_key=base.api_key,
            )
        if operation == "task_decomposition":
            return base

        if operation == "task_assessment":
            # Use base config — needs full token budget for potential subtask output
            return base

        raise ValueError(f"Unknown operation: {operation}")
