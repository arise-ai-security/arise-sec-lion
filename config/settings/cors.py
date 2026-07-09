"""CORS (Cross-Origin Resource Sharing) settings."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CorsConfig(BaseModel):
    """CORS (Cross-Origin Resource Sharing) settings."""

    model_config = {"extra": "forbid"}

    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    allowed_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )
    allowed_headers: list[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True
