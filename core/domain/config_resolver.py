"""Domain service for resolving operation-specific LLM configurations.

This module implements the Strategy pattern for config resolution. It converts
high-level agent configs (which may use different strategies) into concrete
LLMConfig objects for specific operations.

Responsibility:
    Given an AgentConfig and an operation name, return the appropriate LLMConfig
    for that operation. This decouples:
    - What configs the parent specifies (strategy)
    - How configs are resolved for operations (resolution logic)
    - Where configs are used (domain methods)

Design Pattern: Strategy
    Each config strategy (PerOperation, Heuristic, Hybrid) has its own resolution
    logic, but all return the same LLMConfig interface. This makes adding new
    strategies trivial - just add a new model and resolver method.

Architecture Note:
    This is a DOMAIN SERVICE (not application service) because config resolution
    is pure business logic with no infrastructure dependencies. It operates on
    domain value objects and enforces domain invariants.

Reference:
    - Domain Services (DDD): https://www.domainlanguage.com/wp-content/uploads/2016/05/DDD_Reference_2015-03.pdf
"""

from typing import Literal

from core.domain.agent_config import (
    AgentConfig,
    HeuristicConfig,
    HybridConfig,
    LLMConfig,
    PerOperationConfig,
)


# Type alias for supported operations
# This is the set of LLM operations that agents can perform
OperationType = Literal["complexity_evaluation", "task_decomposition"]


class ConfigResolver:
    """Domain service for resolving operation-specific LLM configurations.

    This service implements the Strategy pattern with type-safe Pydantic models.
    It converts high-level AgentConfig (parent's intent) into concrete LLMConfig
    (what to pass to LLM port) for specific operations.

    Adding New Strategies:
        1. Create new Pydantic model in agent_config.py (e.g., MLBasedConfig)
        2. Add to AgentConfig union
        3. Add _resolve_<strategy_name>() method here
        4. Type checker will ensure all strategies are handled

    Usage:
        config = agent.config  # AgentConfig (one of the strategy models)
        llm_config = ConfigResolver.resolve(config, operation="complexity_evaluation")
        response = await llm_port.query(prompt, llm_config.model_dump())
    """

    @staticmethod
    def resolve(config: AgentConfig, operation: OperationType) -> LLMConfig:
        """Resolve LLM config for a specific operation.

        This method dispatches to strategy-specific resolvers based on the
        config's strategy field. Type narrowing via match/case ensures exhaustive
        handling of all strategies.

        Args:
            config: Agent's configuration (one of PerOperation/Heuristic/Hybrid).
            operation: The operation being performed (complexity_evaluation or task_decomposition).

        Returns:
            Validated LLMConfig for the specific operation.

        Raises:
            ValueError: If operation is not supported by the strategy.

        Example:
            >>> config = HeuristicConfig(
            ...     base=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1000),
            ...     tool="claude_code"
            ... )
            >>> llm_config = ConfigResolver.resolve(config, "complexity_evaluation")
            >>> llm_config.temperature
            0.3  # Heuristic lowered temperature
        """
        # Type narrowing with match statement (Python 3.10+)
        # This ensures exhaustive handling - if we add a new strategy and forget
        # to handle it here, the type checker will catch it
        match config:
            case PerOperationConfig():
                return ConfigResolver._resolve_per_operation(config, operation)
            case HeuristicConfig():
                return ConfigResolver._resolve_heuristic(config, operation)
            case HybridConfig():
                return ConfigResolver._resolve_hybrid(config, operation)
            case _:
                # Type checker ensures this is unreachable if all union members handled
                raise ValueError(f"Unknown config strategy: {type(config)}")

    @staticmethod
    def _resolve_per_operation(config: PerOperationConfig, operation: OperationType) -> LLMConfig:
        """Resolve config for per-operation strategy.

        Strategy: Parent has already specified exact config for each operation.
        Just return the appropriate field.

        Args:
            config: PerOperationConfig with configs for each operation.
            operation: The operation to get config for.

        Returns:
            LLMConfig for the requested operation.

        Raises:
            ValueError: Should not happen (all operations have configs in PerOperationConfig).
        """
        if operation == "complexity_evaluation":
            return config.complexity_evaluation
        if operation == "task_decomposition":
            return config.task_decomposition
        # Type system should prevent this, but fail gracefully
        raise ValueError(
            f"Operation '{operation}' not configured in PerOperationConfig. "
            f"Available: complexity_evaluation, task_decomposition"
        )

    @staticmethod
    def _resolve_heuristic(config: HeuristicConfig, operation: OperationType) -> LLMConfig:
        """Resolve config for heuristic strategy.

        Strategy: Apply domain-specific heuristics to base config to derive
        operation-specific configs.

        Heuristics Applied:
            - complexity_evaluation: Lower temperature (0.3) and cap tokens (500)
              because this is a simple classification task (SIMPLE vs COMPLEX).
            - task_decomposition: Use base config as-is because this requires
              full reasoning capability.

        Design Note:
            These heuristics are domain knowledge (not arbitrary magic numbers).
            They encode the understanding that complexity evaluation is simpler
            than task decomposition.

        Args:
            config: HeuristicConfig with base config.
            operation: The operation to derive config for.

        Returns:
            LLMConfig derived from base config via heuristics.

        Raises:
            ValueError: If operation is unknown.
        """
        base = config.base

        if operation == "complexity_evaluation":
            # Heuristic: Complexity evaluation is a simple binary classification
            # (SIMPLE vs COMPLEX). Use lower temperature for more deterministic
            # results and cap tokens since response is short JSON.
            return LLMConfig(
                model=base.model,  # Keep same model
                temperature=0.3,  # Lower than base (more deterministic)
                max_tokens=min(base.max_tokens, 500),  # Cap tokens (response is short)
                top_p=base.top_p,  # Preserve if set
            )
        if operation == "task_decomposition":
            # Heuristic: Task decomposition requires full reasoning capability.
            # Use base config as-is.
            return base
        raise ValueError(f"Unknown operation: {operation}")

    @staticmethod
    def _resolve_hybrid(config: HybridConfig, operation: OperationType) -> LLMConfig:
        """Resolve config for hybrid strategy.

        Strategy: Merge base config with per-operation overrides. Parent specifies
        base config for all operations and can selectively override specific
        parameters for certain operations.

        Merge Logic:
            1. Start with base config dict
            2. Apply operation-specific overrides (if any)
            3. Validate merged config via Pydantic

        Example:
            Base: {model: "gpt-4o", temperature: 0.7, max_tokens: 1000}
            Override for complexity_evaluation: {temperature: 0.3}
            Result: {model: "gpt-4o", temperature: 0.3, max_tokens: 1000}

        Args:
            config: HybridConfig with base config and optional overrides.
            operation: The operation to resolve config for.

        Returns:
            LLMConfig with base + overrides merged.

        Raises:
            ValueError: If merged config is invalid (caught by Pydantic validation).

        Design Note:
            Pydantic validation ensures merged config is valid even if parent
            specifies invalid overrides (e.g., temperature=5.0). This enforces
            domain invariants.
        """
        base = config.base
        overrides = config.overrides.get(operation, {})

        # Merge base with overrides (overrides take precedence)
        merged = base.model_dump()
        merged.update(overrides)

        # Validate merged config via Pydantic
        # This catches invalid overrides (e.g., temperature > 2.0)
        try:
            return LLMConfig(**merged)
        except Exception as e:
            raise ValueError(
                f"Invalid config for operation '{operation}': base={base.model_dump()}, "
                f"overrides={overrides}, error={e}"
            ) from e
