"""Configuration owned by the security domain plugin."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


def _tool_policy() -> dict[str, Any]:
    recon = {
        "max_iterations": 3,
        "toolsets": {
            "recon": {
                "allowed_tools": [
                    "search_codebase",
                    "read_file",
                    "get_file_structure",
                    "get_symbols_overview",
                    "read_symbol",
                ]
            }
        },
    }
    return {role: recon for role in ("boss", "pending", "manager")}


class SecurityPluginConfig(BaseModel):
    """Runtime settings for the SEC-bench plugin."""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    route_policy_version: str = "secbench-manager-recovery-v1"
    tools: list[str] = Field(default_factory=lambda: ["valgrind", "klee"])
    worker_network_mode: Literal["bridge", "host"] = "host"
    worker_docker_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    tools_image_registry: str = "cheshire0814"
    tool_policy: dict[str, Any] = Field(default_factory=_tool_policy)
