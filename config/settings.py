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

    llm_model_boss: str  # Required - e.g., "gpt-4o", "claude-3-5-sonnet-20241022"
    worker_tool_type: Literal["claude_code", "openhands"]  # Required - which worker tool to use
    worker_tool_model: str  # Required - e.g., "openai/gpt-4o" (LiteLLM format)
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

    @staticmethod
    def _filter_yaml_for_env_overrides(
        yaml_data: dict[str, Any],
        env_prefix: str,
    ) -> dict[str, Any]:
        """Remove YAML values that have env var overrides (env vars win)."""
        import os

        result = {}
        for key, value in yaml_data.items():
            env_var = f"{env_prefix}{key.upper()}"
            if os.getenv(env_var) is None:
                result[key] = value
        return result

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

        # Filter out YAML values that have env var overrides
        infra_data = cls._filter_yaml_for_env_overrides(
            config_data.get("infrastructure", {}), "ARISE_INFRA_"
        )
        app_data = cls._filter_yaml_for_env_overrides(
            config_data.get("application", {}), "ARISE_APP_"
        )
        pres_data = cls._filter_yaml_for_env_overrides(
            config_data.get("presentation", {}), "ARISE_UI_"
        )

        return cls(
            infrastructure=InfrastructureSettings(**infra_data),
            application=ApplicationSettings(**app_data),
            presentation=PresentationSettings(**pres_data),
        )

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load from specific YAML file with env var overrides."""
        import yaml

        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open(encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        infra_data = cls._filter_yaml_for_env_overrides(
            config_data.get("infrastructure", {}), "ARISE_INFRA_"
        )
        app_data = cls._filter_yaml_for_env_overrides(
            config_data.get("application", {}), "ARISE_APP_"
        )
        pres_data = cls._filter_yaml_for_env_overrides(
            config_data.get("presentation", {}), "ARISE_UI_"
        )

        return cls(
            infrastructure=InfrastructureSettings(**infra_data),
            application=ApplicationSettings(**app_data),
            presentation=PresentationSettings(**pres_data),
        )
