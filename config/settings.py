"""Configuration Management using Pydantic Settings.

This module provides type-safe configuration management with multiple sources:
1. Default values (config/default.yaml)
2. Environment-specific overrides (config/{ARISE_ENV}.yaml)
3. Environment variables (for secrets and runtime overrides)
4. CLI arguments (runtime overrides via --config flag)

Configuration Cascade (precedence, highest to lowest):
  CLI args > Environment Variables > {env}.yaml > default.yaml > Code defaults

Environment Variable:
    ARISE_ENV: Determines which environment config to load (default: "development")
              Valid values: "development", "production", "test"

Reference:
- 12-Factor App: https://12factor.net/config
- Pydantic Settings: https://docs.pydantic.dev/latest/concepts/pydantic_settings/

Architecture Note:
    Configuration should be separated by concern:
    - Infrastructure: Connection strings, API keys (env vars)
    - Application: Hyperparameters, timeouts (config files)
    - Presentation: UI settings (config files)
"""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Default config directory (relative to project root)
CONFIG_DIR = Path(__file__).parent


class InfrastructureSettings(BaseSettings):
    """Infrastructure configuration (adapters, external services).

    These settings configure external dependencies:
    - Database connections
    - LLM providers
    - Worker tools

    Sources (precedence):
    1. Environment variables (recommended for secrets)
    2. Config file
    3. Defaults
    """

    model_config = SettingsConfigDict(
        env_prefix="ARISE_INFRA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL Event Store
    postgres_host: str = Field(default="localhost", description="PostgreSQL host")
    postgres_port: int = Field(default=5432, description="PostgreSQL port")
    postgres_user: str = Field(default="arise", description="PostgreSQL user")
    postgres_password: str = Field(
        default="arise", description="PostgreSQL password (use env var!)"
    )
    postgres_database: str = Field(default="arise_events", description="PostgreSQL database")

    # LLM Configuration (BOSS Agent Only)
    # Note: LiteLLM automatically reads API keys from standard environment variables:
    #   - OPENAI_API_KEY (for OpenAI models)
    #   - ANTHROPIC_API_KEY (for Claude models)
    #   - GEMINI_API_KEY or GOOGLE_API_KEY (for Gemini models)
    #
    # IMPORTANT: These settings are ONLY used for the root BOSS agent.
    # At runtime, parent agents (BOSS/MANAGER) decide child configurations dynamically
    # via LLM reasoning. Parents specify models, hyperparameters, and tools for each
    # child based on the subtask complexity and requirements.
    #
    # This provides "super flexibility" - different children in the same hierarchy
    # can use different models (GPT-4, Gemini, Claude), different hyperparameters
    # (temperature, max_tokens), and different tools (claude_code, openhands).
    #
    # These role-based defaults are kept for backward compatibility and as
    # examples/documentation of typical model selections:
    llm_model_boss: str = Field(
        default="gpt-4o",
        description="Model for root BOSS agent (high-level planning)",
    )
    llm_model_manager: str = Field(
        default="gpt-4o",
        description=(
            "[DEPRECATED] Example model for MANAGER agents (use parent's config at runtime)"
        ),
    )
    llm_model_worker: str = Field(
        default="gpt-4o-mini",
        description=(
            "[DEPRECATED] Example model for WORKER agents (use parent's config at runtime)"
        ),
    )
    llm_model_pending: str = Field(
        default="gpt-4o-mini",
        description=(
            "[DEPRECATED] Example model for PENDING agents (use parent's config at runtime)"
        ),
    )
    llm_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description=("[DEPRECATED] Example temperature (use parent's config at runtime)"),
    )
    llm_max_tokens: int = Field(
        default=1000,
        gt=0,
        description=("[DEPRECATED] Example max tokens (use parent's config at runtime)"),
    )

    # Worker Tool Configuration
    # NOTE: At runtime, parent agents specify which tool each WORKER child should use
    # via the subtask config. These settings provide the defaults.
    worker_tool_type: Literal["claude_code", "openhands"] = Field(
        default="claude_code",
        description="Default worker tool type (claude_code or openhands)",
    )
    worker_tool_model: str = Field(
        default="openai/gpt-4o",
        description="LiteLLM model identifier for OpenHands worker tool",
    )
    worker_tool_timeout: int = Field(
        default=300,
        gt=0,
        description="Worker tool execution timeout in seconds",
    )

    @property
    def postgres_connection_string(self) -> str:
        """Build PostgreSQL connection string from components."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    def get_model_for_role(self, role: str) -> str:
        """Get the LLM model for a specific agent role.

        Args:
            role: Agent role ("boss", "manager", "worker", "pending").

        Returns:
            Model name for the given role.

        Raises:
            ValueError: If role is unknown.
        """
        role_lower = role.lower()
        if role_lower == "boss":
            return self.llm_model_boss
        if role_lower == "manager":
            return self.llm_model_manager
        if role_lower == "worker":
            return self.llm_model_worker
        if role_lower == "pending":
            return self.llm_model_pending
        raise ValueError(f"Unknown agent role: {role}")


class ApplicationSettings(BaseSettings):
    """Application configuration (business logic, orchestration).

    These settings configure application behavior:
    - Retry logic
    - Timeouts
    - Polling intervals
    - Performance tuning

    Sources (precedence):
    1. Config file
    2. Defaults

    Note: These are hyperparameters, not secrets. They belong in config files,
    not environment variables.
    """

    model_config = SettingsConfigDict(
        env_prefix="ARISE_APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Optimistic Concurrency Control
    max_retries: int = Field(default=3, ge=0, le=10, description="Maximum OCC retry attempts")
    retry_delay: float = Field(default=0.1, ge=0.0, description="Delay between retries (seconds)")

    # Orchestration Loop
    poll_interval: float = Field(
        default=0.5, ge=0.1, description="Agent polling interval (seconds)"
    )

    # Timeouts
    llm_timeout: float = Field(default=30.0, gt=0.0, description="LLM query timeout (seconds)")
    worker_timeout: float = Field(
        default=300.0, gt=0.0, description="Worker task timeout (seconds)"
    )

    # Agent Configuration
    default_task_complexity_threshold: int = Field(
        default=5, ge=1, le=10, description="Complexity threshold for task decomposition"
    )


class PresentationSettings(BaseSettings):
    """Presentation configuration (UI, logging, output).

    These settings configure user-facing behavior:
    - Verbosity
    - Output formats
    - Logging
    """

    model_config = SettingsConfigDict(
        env_prefix="ARISE_UI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    verbose: bool = Field(default=True, description="Enable verbose output")
    show_progress: bool = Field(default=True, description="Show progress indicators")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", description="Logging level"
    )
    output_directory: str = Field(
        default="./output",
        description="Directory for generated code, exports, and last run tracking",
    )


class Settings(BaseSettings):
    """Root configuration container.

    This aggregates all configuration sections:
    - Infrastructure (external dependencies)
    - Application (business logic)
    - Presentation (user interface)

    Usage:
        # Load from defaults + env vars
        settings = Settings()

        # Load from config file
        settings = Settings.from_yaml("config/production.yaml")

        # Access nested settings
        print(settings.infrastructure.postgres_connection_string)
        print(settings.application.max_retries)
    """

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
        """Deep merge two dictionaries, with override taking precedence.

        Args:
            base: Base dictionary (default values).
            override: Override dictionary (environment-specific values).

        Returns:
            Merged dictionary with override values taking precedence.
        """
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
        """Remove fields from data that have environment variable overrides.

        In pydantic-settings, init kwargs take precedence over env vars.
        To ensure env vars can override YAML config, we must NOT pass
        fields that have corresponding env vars set.

        Args:
            data: Dictionary of field values (from YAML).
            env_prefix: Environment variable prefix (e.g., "ARISE_INFRA_").
            field_names: List of field names to check.

        Returns:
            Filtered dictionary with env-overridden fields removed.
        """
        result = data.copy()
        for field_name in field_names:
            env_var = f"{env_prefix}{field_name.upper()}"
            if os.getenv(env_var) is not None and field_name in result:
                del result[field_name]
        return result

    @classmethod
    def load(cls, env: str | None = None, config_dir: Path | None = None) -> "Settings":
        """Load settings with cascade: {env}.yaml -> default.yaml.

        This implements the environment-based configuration pattern:
        1. Load config/default.yaml as base
        2. Load config/{env}.yaml and merge (overrides default)
        3. Environment variables still take final precedence

        Args:
            env: Environment name (default: ARISE_ENV or "development").
                 Valid values: "development", "production", "test"
            config_dir: Directory containing config files (default: config/).

        Returns:
            Settings instance with merged configuration.

        Example:
            # Uses ARISE_ENV environment variable (or "development" if not set)
            settings = Settings.load()

            # Explicitly specify environment
            settings = Settings.load(env="production")
        """
        import yaml

        # Determine environment
        if env is None:
            env = os.getenv("ARISE_ENV", "development")

        # Determine config directory
        if config_dir is None:
            config_dir = CONFIG_DIR

        # Load default.yaml as base
        default_path = config_dir / "default.yaml"
        config_data: dict[str, Any] = {}

        if default_path.exists():
            with default_path.open(encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}

        # Load environment-specific config and merge
        env_path = config_dir / f"{env}.yaml"
        if env_path.exists():
            with env_path.open(encoding="utf-8") as f:
                env_data = yaml.safe_load(f) or {}
            config_data = cls._deep_merge(config_data, env_data)

        # Create nested settings with proper env var precedence
        #
        # IMPORTANT: In pydantic-settings, init kwargs have HIGHEST priority,
        # even higher than environment variables. To ensure env vars can
        # override YAML config, we filter out fields that have env vars set.
        #
        # Priority (after filtering):
        #   1. Environment variables (for fields not in filtered data)
        #   2. YAML config values (for fields not overridden by env vars)
        #   3. Code defaults (for fields not in YAML)
        infra_data = config_data.get("infrastructure", {})
        app_data = config_data.get("application", {})
        pres_data = config_data.get("presentation", {})

        # Filter out fields that have env var overrides
        infra_fields = list(InfrastructureSettings.model_fields.keys())
        app_fields = list(ApplicationSettings.model_fields.keys())
        pres_fields = list(PresentationSettings.model_fields.keys())

        filtered_infra = cls._filter_env_overridden(infra_data, "ARISE_INFRA_", infra_fields)
        filtered_app = cls._filter_env_overridden(app_data, "ARISE_APP_", app_fields)
        filtered_pres = cls._filter_env_overridden(pres_data, "ARISE_UI_", pres_fields)

        # Create nested settings - env vars will now take precedence
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
        """Load settings from a specific YAML file (no cascade).

        Use this when you want to load from a specific file without
        the environment-based cascade. For cascade loading, use Settings.load().

        Args:
            config_path: Path to YAML configuration file.

        Returns:
            Settings instance with values from YAML file.

        Note:
            Environment variables still take precedence over file values.
            This allows secrets to be in env vars while config is in files.
        """
        import yaml

        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open(encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        # Create settings with data from file
        # Pydantic Settings will still check env vars and override file values
        return cls(**config_data)

    @classmethod
    def from_toml(cls, config_path: str | Path) -> "Settings":
        """Load settings from TOML file.

        Args:
            config_path: Path to TOML configuration file.

        Returns:
            Settings instance with values from TOML file.
        """
        import tomli

        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open("rb") as f:
            config_data = tomli.load(f)

        return cls(**config_data)
