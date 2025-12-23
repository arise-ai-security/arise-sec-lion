"""Domain service for resolving operation-specific LLM configurations."""

from typing import Literal

from core.domain.agent_config import (
    AgentConfig,
    HeuristicConfig,
    HybridConfig,
    LLMConfig,
    PerOperationConfig,
)


OperationType = Literal["complexity_evaluation", "task_decomposition"]


class ConfigResolver:
    """Resolves AgentConfig to LLMConfig for specific operations."""

    @staticmethod
    def resolve(config: AgentConfig, operation: OperationType) -> LLMConfig:
        """Dispatch to strategy-specific resolver."""
        match config:
            case PerOperationConfig():
                return ConfigResolver._resolve_per_operation(config, operation)
            case HeuristicConfig():
                return ConfigResolver._resolve_heuristic(config, operation)
            case HybridConfig():
                return ConfigResolver._resolve_hybrid(config, operation)
            case _:
                raise ValueError(f"Unknown config strategy: {type(config)}")

    @staticmethod
    def _resolve_per_operation(config: PerOperationConfig, operation: OperationType) -> LLMConfig:
        """Return exact config specified for operation."""
        if operation == "complexity_evaluation":
            return config.complexity_evaluation
        if operation == "task_decomposition":
            return config.task_decomposition
        raise ValueError(f"Unknown operation: {operation}")

    @staticmethod
    def _resolve_heuristic(config: HeuristicConfig, operation: OperationType) -> LLMConfig:
        """Apply heuristics: lower temp for complexity_evaluation, base for decomposition."""
        base = config.base
        if operation == "complexity_evaluation":
            return LLMConfig(
                model=base.model,
                temperature=0.3,
                max_tokens=min(base.max_tokens, 1000),
                top_p=base.top_p,
            )
        if operation == "task_decomposition":
            return base
        raise ValueError(f"Unknown operation: {operation}")

    @staticmethod
    def _resolve_hybrid(config: HybridConfig, operation: OperationType) -> LLMConfig:
        """Merge base config with per-operation overrides."""
        base = config.base
        overrides = config.overrides.get(operation, {})
        merged = base.model_dump()
        merged.update(overrides)
        try:
            return LLMConfig(**merged)
        except Exception as e:
            raise ValueError(f"Invalid config for '{operation}': {e}") from e
