"""Configuration via Pydantic Settings: YAML + env vars, cascade loading."""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


CONFIG_DIR = Path(__file__).parent


class InfrastructureSettings(BaseSettings):
    """DB, LLM, and worker tool settings. Env prefix: ARISE_INFRA_"""

    model_config = SettingsConfigDict(
        env_prefix="ARISE_INFRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = Field(default="localhost")
    postgres_port: int = Field(default=5432)
    postgres_user: str = Field(default="arise")
    postgres_password: str = Field(default="arise")
    postgres_database: str = Field(default="arise_events")

    llm_model_boss: str = Field(default="gpt-4o")
    worker_tool_type: Literal["claude_code", "openhands"] = Field(default="claude_code")
    worker_tool_model: str = Field(default="openai/gpt-4o")
    worker_tool_timeout: int = Field(default=300, gt=0)

    @property
    def postgres_connection_string(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )


class ApplicationSettings(BaseSettings):
    """Orchestration settings: retries, timeouts, polling. Env prefix: ARISE_APP_"""

    model_config = SettingsConfigDict(
        env_prefix="ARISE_APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=0.1, ge=0.0)
    poll_interval: float = Field(default=0.5, ge=0.1)
    llm_timeout: float = Field(default=30.0, gt=0.0)
    worker_timeout: float = Field(default=300.0, gt=0.0)
    default_task_complexity_threshold: int = Field(default=5, ge=1, le=10)


class PresentationSettings(BaseSettings):
    """UI settings: verbosity, logging, output. Env prefix: ARISE_UI_"""

    model_config = SettingsConfigDict(
        env_prefix="ARISE_UI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    verbose: bool = Field(default=True)
    show_progress: bool = Field(default=True)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    output_directory: str = Field(default="./output")


class Settings(BaseSettings):
    """Root config: infrastructure + application + presentation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    infrastructure: InfrastructureSettings = Field(default_factory=InfrastructureSettings)
    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    presentation: PresentationSettings = Field(default_factory=PresentationSettings)

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = Settings._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _filter_env_overridden(
        data: dict[str, Any],
        env_prefix: str,
        field_names: list[str],
    ) -> dict[str, Any]:
        """Remove fields that have env var overrides (env vars take precedence)."""
        result = data.copy()
        for field_name in field_names:
            env_var = f"{env_prefix}{field_name.upper()}"
            if os.getenv(env_var) is not None and field_name in result:
                del result[field_name]
        return result

    @classmethod
    def load(cls, env: str | None = None, config_dir: Path | None = None) -> "Settings":
        """Load with cascade: {env}.yaml → default.yaml → env vars."""
        import yaml

        if env is None:
            env = os.getenv("ARISE_ENV", "development")
        if config_dir is None:
            config_dir = CONFIG_DIR

        default_path = config_dir / "default.yaml"
        config_data: dict[str, Any] = {}
        if default_path.exists():
            with default_path.open(encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}

        env_path = config_dir / f"{env}.yaml"
        if env_path.exists():
            with env_path.open(encoding="utf-8") as f:
                env_data = yaml.safe_load(f) or {}
            config_data = cls._deep_merge(config_data, env_data)

        infra_data = config_data.get("infrastructure", {})
        app_data = config_data.get("application", {})
        pres_data = config_data.get("presentation", {})

        infra_fields = list(InfrastructureSettings.model_fields.keys())
        app_fields = list(ApplicationSettings.model_fields.keys())
        pres_fields = list(PresentationSettings.model_fields.keys())

        filtered_infra = cls._filter_env_overridden(infra_data, "ARISE_INFRA_", infra_fields)
        filtered_app = cls._filter_env_overridden(app_data, "ARISE_APP_", app_fields)
        filtered_pres = cls._filter_env_overridden(pres_data, "ARISE_UI_", pres_fields)

        infrastructure = InfrastructureSettings(**filtered_infra)
        application = ApplicationSettings(**filtered_app)
        presentation = PresentationSettings(**filtered_pres)

        return cls(
            infrastructure=infrastructure,
            application=application,
            presentation=presentation,
        )

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load from specific YAML (no cascade). Env vars still override."""
        import yaml

        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open(encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)

    @classmethod
    def from_toml(cls, config_path: str | Path) -> "Settings":
        """Load from TOML file."""
        import tomli

        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open("rb") as f:
            config_data = tomli.load(f)

        return cls(**config_data)
