"""Type-safe configuration models for agent LLM operations.

This module defines Pydantic models for agent configuration with multiple
strategies for config resolution. These are value objects per DDD principles:
immutable, self-validating, and defined by their attributes.

Strategy Pattern:
    The system supports multiple strategies for deciding which model/hyperparameters
    to use for different LLM operations:

    1. PerOperationConfig: Parent specifies exact config for each operation
    2. HeuristicConfig: Agent applies domain rules to derive configs
    3. HybridConfig: Base config with optional per-operation overrides

    This makes the system future-proof - new strategies can be added without
    refactoring existing code.

Usage:
    # Parent creates config for child
    config = PerOperationConfig(
        complexity_evaluation=LLMConfig(model="gpt-4o-mini", temperature=0.3, max_tokens=300),
        task_decomposition=LLMConfig(model="gpt-4o", temperature=0.7, max_tokens=1500),
        tool="claude_code"
    )

    # Domain model resolves operation-specific config
    llm_config = ConfigResolver.resolve(config, operation="complexity_evaluation")

Reference:
    - Strategy Pattern: https://refactoring.guru/design-patterns/strategy
    - Pydantic Discriminated Unions: https://docs.pydantic.dev/latest/concepts/unions/
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """Configuration for a single LLM operation.

    This is a value object representing the hyperparameters needed to call
    an LLM provider (OpenAI, Anthropic, Google, etc.) via LiteLLM.

    Value Object Properties:
        - Immutable (frozen=True)
        - Self-validating (Pydantic constraints)
        - Defined by attributes (no identity)

    Attributes:
        model: LLM model identifier (e.g., "gpt-4o", "claude-3-5-sonnet-20241022", "gemini-pro").
        temperature: Sampling temperature. Higher = more random, lower = more deterministic.
                    Typically 0.0-1.0, but some models support up to 2.0.
        max_tokens: Maximum tokens to generate in response.
        top_p: Nucleus sampling parameter. Alternative to temperature.
               If None, provider default used.
    """

    model_config = {"frozen": True}  # Immutable value object

    model: str = Field(
        ..., min_length=1, description="LLM model name (e.g., 'gpt-4o', 'gemini-pro')"
    )
    temperature: float = Field(
        ...,
        ge=0.0,
        le=2.0,
        description="Sampling temperature (0.0-2.0, lower=deterministic)",
    )
    max_tokens: int = Field(..., gt=0, le=100000, description="Maximum tokens to generate")
    top_p: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Nucleus sampling parameter (optional)"
    )


class PerOperationConfig(BaseModel):
    """Config strategy where parent specifies exact config for each operation.

    The parent's LLM analyzes the subtask and decides optimal model/hyperparameters
    for each operation the child might perform. This provides maximum flexibility
    but requires the parent to reason about all possible child operations.

    Example Use Case:
        Parent knows that complexity evaluation is a simple classification task
        (use cheap, low-temperature model) while task decomposition requires
        creative reasoning (use powerful, higher-temperature model).

    Attributes:
        strategy: Discriminator field for Pydantic union (always "per_operation").
        complexity_evaluation: Config for evaluating whether task is SIMPLE or COMPLEX.
        task_decomposition: Config for decomposing task into subtasks (if child becomes MANAGER).
        tool: Worker tool to use for execution (if child becomes WORKER).
    """

    model_config = {"frozen": True}

    strategy: Literal["per_operation"] = "per_operation"

    complexity_evaluation: LLMConfig = Field(
        ..., description="Config for evaluating task complexity (SIMPLE vs COMPLEX)"
    )
    task_decomposition: LLMConfig = Field(
        ..., description="Config for decomposing task into subtasks (if child is MANAGER)"
    )
    tool: Literal["claude_code", "openhands"] = Field(
        default="claude_code", description="Worker tool for task execution"
    )


class HeuristicConfig(BaseModel):
    """Config strategy where agent applies hardcoded domain heuristics.

    The parent specifies one base config, and the agent applies domain-specific
    rules to derive operation-specific configs. This is simpler for the parent
    but less flexible than per-operation config.

    Example Heuristics:
        - Complexity evaluation: Use base model but lower temperature to 0.3
        - Task decomposition: Use base config as-is

    Attributes:
        strategy: Discriminator field for Pydantic union (always "heuristic").
        base: Base LLM config to apply heuristics to.
        tool: Worker tool to use for execution.
    """

    model_config = {"frozen": True}

    strategy: Literal["heuristic"] = "heuristic"

    base: LLMConfig = Field(..., description="Base config to apply heuristics to")
    tool: Literal["claude_code", "openhands"] = Field(
        default="claude_code", description="Worker tool for task execution"
    )


class HybridConfig(BaseModel):
    """Config strategy with base config plus optional per-operation overrides.

    The parent specifies a base config and can selectively override specific
    parameters for certain operations. This balances simplicity (one base config)
    with flexibility (targeted overrides).

    Example:
        Base: {"model": "gpt-4o", "temperature": 0.7, "max_tokens": 1000}
        Override for complexity_evaluation: {"temperature": 0.3, "max_tokens": 300}
        Result: gpt-4o with temp=0.3, max_tokens=300 for complexity eval

    Attributes:
        strategy: Discriminator field for Pydantic union (always "hybrid").
        base: Base LLM config for all operations.
        overrides: Per-operation parameter overrides (merged with base).
                  Keys are operation names, values are dicts with parameter overrides.
        tool: Worker tool to use for execution.
    """

    model_config = {"frozen": True}

    strategy: Literal["hybrid"] = "hybrid"

    base: LLMConfig = Field(..., description="Base config for all operations")
    overrides: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per-operation parameter overrides (merged with base)",
    )
    tool: Literal["claude_code", "openhands"] = Field(
        default="claude_code", description="Worker tool for task execution"
    )


# Discriminated union for type-safe strategy selection
# Pydantic will discriminate based on the "strategy" field
AgentConfig = PerOperationConfig | HeuristicConfig | HybridConfig
