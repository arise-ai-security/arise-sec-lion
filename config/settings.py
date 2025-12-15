"""Configuration via Pydantic Settings with phase-specific YAML support.

Design principle: Secrets in .env, everything else in YAML.

Load hierarchy (highest to lowest priority):
1. Environment variables (secrets + Docker overrides only)
2. config.{ARISE_ENV}.yaml (phase-specific)
3. config.yaml (base defaults)

Usage:
    from config import Settings
    settings = Settings.load()  # Uses ARISE_ENV to determine phase
"""

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


CONFIG_DIR = Path(__file__).parent


def get_environment() -> str:
    """Get current environment from ARISE_ENV (default: development)."""
    return os.getenv("ARISE_ENV", "development")


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base (override wins)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_hierarchy(env: str | None = None) -> dict[str, Any]:
    """Load and merge YAML configs: base <- phase-specific."""
    if env is None:
        env = get_environment()

    # Load base config
    base_path = CONFIG_DIR / "config.yaml"
    config: dict[str, Any] = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    # Merge phase-specific config (if exists)
    phase_path = CONFIG_DIR / f"config.{env}.yaml"
    if phase_path.exists():
        with phase_path.open(encoding="utf-8") as f:
            phase_config = yaml.safe_load(f) or {}
            config = _merge_dicts(config, phase_config)

    return config


# =============================================================================
# Configuration Models (BaseModel - no env var reading)
# =============================================================================


class InfrastructureConfig(BaseModel):
    """DB, LLM, and worker tool settings. Loaded from YAML."""

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "arise"
    postgres_database: str = "arise_events"

    llm_model_boss: str  # Required in YAML
    worker_tool_type: Literal["claude_code", "openhands"]  # Required in YAML
    worker_tool_model: str  # Required in YAML
    worker_tool_timeout: int = Field(default=300, gt=0)


class ApplicationConfig(BaseModel):
    """Orchestration settings. Loaded from YAML."""

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=0.1, ge=0.0)
    poll_interval: float = Field(default=0.5, ge=0.1)
    llm_timeout: float = Field(default=30.0, gt=0.0)
    worker_timeout: float = Field(default=300.0, gt=0.0)
    default_task_complexity_threshold: int = Field(default=5, ge=1, le=10)


class PresentationConfig(BaseModel):
    """UI settings. Loaded from YAML."""

    verbose: bool = True
    show_progress: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    output_directory: str = "./output"


# =============================================================================
# Root Settings (BaseSettings - reads secrets from env)
# =============================================================================


class Settings(BaseSettings):
    """Root config: secrets from env, everything else from YAML.

    Env vars (secrets only):
        POSTGRES_PASSWORD - Database password
        POSTGRES_HOST - Docker override for database host
        OPENAI_API_KEY - OpenAI API key (read by LiteLLM)
        ANTHROPIC_API_KEY - Anthropic API key (read by LiteLLM)
    """

    model_config = SettingsConfigDict(extra="ignore")

    # --- Secrets from environment ---
    postgres_password: str = Field(description="Database password from POSTGRES_PASSWORD")
    postgres_host: str | None = Field(
        default=None, description="Override from POSTGRES_HOST (for Docker)"
    )

    # --- Config sections from YAML ---
    infrastructure: InfrastructureConfig = Field(
        default_factory=lambda: InfrastructureConfig(
            llm_model_boss="gpt-4o",
            worker_tool_type="openhands",
            worker_tool_model="openai/gpt-4o",
        )
    )
    application: ApplicationConfig = Field(default_factory=ApplicationConfig)
    presentation: PresentationConfig = Field(default_factory=PresentationConfig)

    @computed_field
    @property
    def postgres_connection_string(self) -> str:
        """Build connection string from config + secret."""
        host = self.postgres_host or self.infrastructure.postgres_host
        return (
            f"postgresql://{self.infrastructure.postgres_user}:{self.postgres_password}"
            f"@{host}:{self.infrastructure.postgres_port}/{self.infrastructure.postgres_database}"
        )

    @classmethod
    def load(cls, env: str | None = None) -> "Settings":
        """Load settings: YAML config + env secrets.

        Args:
            env: Override ARISE_ENV (default: read from environment)

        Returns:
            Settings instance with merged configuration
        """
        config = _load_yaml_hierarchy(env)

        return cls(
            infrastructure=InfrastructureConfig(**config.get("infrastructure", {})),
            application=ApplicationConfig(**config.get("application", {})),
            presentation=PresentationConfig(**config.get("presentation", {})),
        )

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load from specific YAML file (for testing)."""
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        return cls(
            infrastructure=InfrastructureConfig(**config.get("infrastructure", {})),
            application=ApplicationConfig(**config.get("application", {})),
            presentation=PresentationConfig(**config.get("presentation", {})),
        )


# =============================================================================
# Backwards Compatibility Aliases
# =============================================================================

# These aliases maintain backwards compatibility with existing code
InfrastructureSettings = InfrastructureConfig
ApplicationSettings = ApplicationConfig
PresentationSettings = PresentationConfig
