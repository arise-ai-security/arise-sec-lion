"""SEC-bench container settings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SecurityConfig(BaseModel):
    """SEC-bench container settings."""

    model_config = {"extra": "forbid"}

    enabled: bool = True  # Uses worker.timeout for secb commands
    tools: list[str] = Field(
        default_factory=lambda: ["valgrind"],
        description="Security analysis tools to enable in SEC-bench containers",
    )
    worker_network_mode: Literal["bridge", "host"] = "host"
    worker_docker_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    tools_image_registry: str = Field(
        default="",
        description=(
            "Registry namespace hosting prebuilt secb-tools images "
            "(e.g. 'cheshire0814' or 'ghcr.io/org'). When set, a run that cannot "
            "find secb-tools:<tag> locally pulls <registry>/secb-tools:<tag> "
            "and retags it, so fresh machines run without building. Empty "
            "disables the fallback (local build only)."
        ),
    )
