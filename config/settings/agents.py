"""LLM settings for the boss/manager agent tiers and the format repairer."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BossConfig(BaseModel):
    """Boss agent LLM settings."""

    model_config = {"extra": "forbid"}

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description=(
            "Optional LiteLLM api_base override (e.g. https://ollama.com for Ollama Cloud)."
        ),
    )


class ManagerConfig(BaseModel):
    """Manager agent LLM settings."""

    model_config = {"extra": "forbid"}

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description=(
            "Optional LiteLLM api_base override (e.g. https://ollama.com for Ollama Cloud)."
        ),
    )


class FormatRepairerConfig(BaseModel):
    """Fallback LLM-backed output-format repairer settings.

    Used when the deterministic ``raw_decode`` / ``_repair_json`` chain
    fails on output from less-disciplined models (qwen3, deepseek, GLM,
    unknown). The repair model must be configured by YAML; there is no
    source-code model fallback.

    Distinct from the security-domain "Fixer" agent role — this repairs
    output format, never source code.
    """

    model_config = {"extra": "forbid"}

    # Disabled by default. Enable when running on models prone to malformed
    # output (qwen3, qwen3.5, deepseek, GLM, …). Adds a dependency on the
    # configured repair model and a one-shot LLM call per parse failure.
    # GPT/Claude paths are unaffected when enabled.
    enabled: bool = False
    model: str = Field(min_length=1)
    # Default 16000 to match boss/manager.max_tokens — a repaired output
    # cannot need more space than the source model could have produced.
    # Empirical max from prior runs: ~5000 tokens; p99 ~3500.
    max_tokens: int = Field(default=16000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description="Optional LiteLLM api_base override for the repairer.",
    )
    # Bound concurrent repair calls so a parse-failure storm cannot
    # amplify into N simultaneous LLM requests against the repair
    # endpoint. Sized to host capacity (Ollama Cloud or local Ollama),
    # not to source-model concurrency.
    max_concurrent: int = Field(default=3, ge=1, le=32)
