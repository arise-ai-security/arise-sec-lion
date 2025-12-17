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
from pydantic import BaseModel, Field
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
    """DB, LLM, and worker tool settings.

    All fields are required - missing values will raise an error.
    Postgres password injected from POSTGRES_PASSWORD env var in Settings.load().
    """

    # Postgres settings (all required)
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str  # Injected from POSTGRES_PASSWORD env var
    postgres_database: str

    # LLM settings (all required)
    llm_model_boss: str
    worker_tool_type: Literal["claude_code", "openhands"]
    worker_tool_model: str
    worker_tool_timeout: int

    @property
    def postgres_connection_string(self) -> str:
        """Build PostgreSQL connection string."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )


class BudgetConfig(BaseModel):
    """Cost and token limits. Hard stops to prevent runaway spending."""

    max_total_cost_usd: float = Field(default=10.0, ge=0.0)
    max_tokens_per_agent: int = Field(default=100000, gt=0)
    cost_warning_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    cost_tracking_enabled: bool = True


class ApplicationConfig(BaseModel):
    """Orchestration settings. Loaded from YAML."""

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay: float = Field(default=0.1, ge=0.0)
    poll_interval: float = Field(default=0.5, ge=0.1)
    llm_timeout: float = Field(default=30.0, gt=0.0)
    worker_timeout: float = Field(default=300.0, gt=0.0)
    default_task_complexity_threshold: int = Field(default=5, ge=1, le=10)

    # Budget configuration for cost tracking and limits
    budget: BudgetConfig


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

    Env vars injected into infrastructure config in load():
        POSTGRES_PASSWORD - Database password
        POSTGRES_HOST - Optional override for Docker

    Env vars read directly by LiteLLM (not managed here):
        OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
    """

    model_config = SettingsConfigDict(extra="ignore")

    # --- Config sections from YAML (required - no defaults) ---
    infrastructure: InfrastructureConfig
    application: ApplicationConfig
    presentation: PresentationConfig

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> "Settings":
        """Build Settings from config dict, injecting env vars into infrastructure.

        Env vars injected:
            POSTGRES_PASSWORD - Required database password
            POSTGRES_HOST - Optional override (for Docker)

        Raises:
            ValueError: If POSTGRES_PASSWORD env var is not set
            ValidationError: If required fields are missing
        """
        infra_config = config.get("infrastructure", {})

        # Inject postgres secrets from environment
        postgres_password = os.getenv("POSTGRES_PASSWORD")
        if not postgres_password:
            raise ValueError("POSTGRES_PASSWORD environment variable is required")
        infra_config["postgres_password"] = postgres_password

        # Optional host override for Docker
        postgres_host_override = os.getenv("POSTGRES_HOST")
        if postgres_host_override:
            infra_config["postgres_host"] = postgres_host_override

        return cls(
            infrastructure=InfrastructureConfig(**infra_config),
            application=ApplicationConfig(**config.get("application", {})),
            presentation=PresentationConfig(**config.get("presentation", {})),
        )

    @classmethod
    def load(cls, env: str | None = None) -> "Settings":
        """Load settings: YAML config + env secrets.

        Args:
            env: Override ARISE_ENV (default: read from environment)

        Returns:
            Settings instance with merged configuration

        Raises:
            ValueError: If POSTGRES_PASSWORD env var is not set
            ValidationError: If required fields are missing from config.yaml
        """
        config = _load_yaml_hierarchy(env)
        return cls._build_from_config(config)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load from specific YAML file (for testing).

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If POSTGRES_PASSWORD env var is not set
            ValidationError: If required fields are missing
        """
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        return cls._build_from_config(config)
