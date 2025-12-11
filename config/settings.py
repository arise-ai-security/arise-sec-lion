"""Configuration via Pydantic Settings: single YAML + env var overrides."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


CONFIG_DIR = Path(__file__).parent


class InfrastructureSettings(BaseSettings):
    """DB, LLM, and worker tool settings. Env prefix: ARISE_INFRA_"""

    model_config = SettingsConfigDict(
        env_prefix="ARISE_INFRA_",
        extra="ignore",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "arise"
    postgres_password: str  # Required - set via ARISE_INFRA_POSTGRES_PASSWORD or .env
    postgres_database: str = "arise"

    llm_model_boss: str = "gpt-4o"
    worker_tool_type: Literal["claude_code", "openhands"] = "openhands"
    worker_tool_model: str = "openai/gpt-4o"
    worker_tool_timeout: int = Field(default=300, gt=0)

    @property
    def postgres_connection_string(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )


class ApplicationSettings(BaseSettings):
    """Orchestration settings. Env prefix: ARISE_APP_"""

    model_config = SettingsConfigDict(
        env_prefix="ARISE_APP_",
        extra="ignore",
    )

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=0.1, ge=0.0)
    poll_interval: float = Field(default=0.5, ge=0.1)
    llm_timeout: float = Field(default=30.0, gt=0.0)
    worker_timeout: float = Field(default=300.0, gt=0.0)
    default_task_complexity_threshold: int = Field(default=5, ge=1, le=10)


class PresentationSettings(BaseSettings):
    """UI settings. Env prefix: ARISE_UI_"""

    model_config = SettingsConfigDict(
        env_prefix="ARISE_UI_",
        extra="ignore",
    )

    verbose: bool = True
    show_progress: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    output_directory: str = "./output"


class Settings(BaseSettings):
    """Root config: infrastructure + application + presentation."""

    model_config = SettingsConfigDict(extra="ignore")

    infrastructure: InfrastructureSettings = Field(default_factory=InfrastructureSettings)
    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    presentation: PresentationSettings = Field(default_factory=PresentationSettings)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Settings":
        """Load from config.yaml with env var overrides."""
        import yaml

        if config_dir is None:
            config_dir = CONFIG_DIR

        config_path = config_dir / "config.yaml"
        config_data: dict[str, Any] = {}

        if config_path.exists():
            with config_path.open(encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}

        # Build settings - env vars automatically override YAML values
        return cls(
            infrastructure=InfrastructureSettings(**config_data.get("infrastructure", {})),
            application=ApplicationSettings(**config_data.get("application", {})),
            presentation=PresentationSettings(**config_data.get("presentation", {})),
        )
