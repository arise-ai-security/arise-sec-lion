"""UI and output settings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class OutputConfig(BaseModel):
    """UI and output settings."""

    model_config = {"extra": "forbid"}

    verbose: bool
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    directory: str
