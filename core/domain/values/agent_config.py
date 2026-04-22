"""Agent configuration models - simplified to heuristic strategy only."""

from typing import Literal

from pydantic import BaseModel, Field


VALID_WORKER_TOOLS: set[str] = {"claude_code", "openhands", "google_adk"}
DEFAULT_WORKER_TOOL: str = "claude_code"


class LLMConfig(BaseModel):
    """LLM hyperparameters for a single operation."""

    model_config = {"frozen": True}

    model: str = Field(..., min_length=1)
    temperature: float = Field(..., ge=0.0, le=2.0)
    max_tokens: int = Field(..., gt=0, le=100000)
    # Optional per-call overrides for LiteLLM. When None, LiteLLM falls back to
    # provider-specific env vars (e.g. OLLAMA_API_BASE, OLLAMA_API_KEY).
    api_base: str | None = Field(default=None)
    api_key: str | None = Field(default=None)


class HeuristicConfig(BaseModel):
    """Strategy: agent applies domain heuristics to base config."""

    model_config = {"frozen": True}

    strategy: Literal["heuristic"] = "heuristic"
    base: LLMConfig
    tool: Literal["claude_code", "openhands", "google_adk"] = "claude_code"


type AgentConfig = HeuristicConfig
