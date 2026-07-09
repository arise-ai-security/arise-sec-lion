"""PostgreSQL connection settings."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """PostgreSQL connection settings."""

    model_config = {"extra": "forbid"}

    host: str
    port: int
    user: str
    password: str
    name: str
    # asyncpg pool sizing (G.1). Default matches the historical asyncpg
    # defaults of (min=10, max=10) so unchanged deployments behave the
    # same; raise pool_max to widen the connection pool under load.
    pool_min: int = Field(default=10, ge=1, le=100)
    pool_max: int = Field(default=10, ge=1, le=100)

    @property
    def connection_string(self) -> str:
        """Build PostgreSQL connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
