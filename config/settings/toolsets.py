"""Per-role and per-domain toolset policies and tool-calling loop settings."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolsetPolicyConfig(BaseModel):
    """Per-toolset enablement and allowed-tool overrides."""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    allowed_tools: list[str] | None = None


class ToolsetRoleConfig(BaseModel):
    """Per-role tool-calling policy defaults."""

    model_config = {"extra": "forbid"}

    max_iterations: int = 5
    result_char_limit: int = 6_000
    toolsets: dict[str, ToolsetPolicyConfig] = Field(default_factory=dict)


class ToolsetConfig(BaseModel):
    """Per-role + per-domain toolset policy config.

    Shape::

        toolsets:
          default:
            pending:
              max_iterations: 5
              toolsets:
                recon: {enabled: true}
            manager:
              max_iterations: 5
              toolsets:
                recon: {enabled: true}
            boss:
              toolsets:
                recon: {enabled: false}
          domains:
            <domain>:
              manager:
                max_iterations: 3
                toolsets:
                  recon: {allowed_tools: [...]}
    """

    model_config = {"extra": "forbid"}

    default: dict[str, ToolsetRoleConfig] = Field(default_factory=dict)
    domains: dict[str, dict[str, ToolsetRoleConfig]] = Field(default_factory=dict)

    def to_raw_dict(self) -> dict[str, Any]:
        """Convert to the normalized dict format consumed by the resolver."""
        return self.model_dump(mode="python")


class ToolCallingConfig(BaseModel):
    """Tool-calling loop defaults and per-role policies."""

    model_config = {"extra": "forbid"}

    max_iterations: int = 10
    result_char_limit: int = 6_000
    condense_after_iteration: int = 2
    token_budget: int = 80_000
    policies: ToolsetConfig = Field(default_factory=ToolsetConfig)
