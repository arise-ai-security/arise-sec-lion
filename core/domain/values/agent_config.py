"""Agent configuration models with multiple resolution strategies."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """LLM hyperparameters for a single operation."""

    model_config = {"frozen": True}

    model: str = Field(..., min_length=1)
    temperature: float = Field(..., ge=0.0, le=2.0)
    max_tokens: int = Field(..., gt=0, le=100000)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)


class PerOperationConfig(BaseModel):
    """Strategy: parent specifies exact config per operation."""

    model_config = {"frozen": True}

    strategy: Literal["per_operation"] = "per_operation"
    complexity_evaluation: LLMConfig
    task_decomposition: LLMConfig
    tool: Literal["claude_code", "openhands", "google_adk"] = "claude_code"


class HeuristicConfig(BaseModel):
    """Strategy: agent applies domain heuristics to base config."""

    model_config = {"frozen": True}

    strategy: Literal["heuristic"] = "heuristic"
    base: LLMConfig
    tool: Literal["claude_code", "openhands", "google_adk"] = "claude_code"


class HybridConfig(BaseModel):
    """Strategy: base config with optional per-operation overrides."""

    model_config = {"frozen": True}

    strategy: Literal["hybrid"] = "hybrid"
    base: LLMConfig
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tool: Literal["claude_code", "openhands", "google_adk"] = "claude_code"


AgentConfig = PerOperationConfig | HeuristicConfig | HybridConfig
