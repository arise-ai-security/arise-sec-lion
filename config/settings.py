"""Configuration via Pydantic Settings with phase-specific YAML support."""

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

    base_path = CONFIG_DIR / "config.yaml"
    config: dict[str, Any] = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    phase_path = CONFIG_DIR / f"config.{env}.yaml"
    if phase_path.exists():
        with phase_path.open(encoding="utf-8") as f:
            phase_config = yaml.safe_load(f) or {}
            config = _merge_dicts(config, phase_config)

    return config


class DatabaseConfig(BaseModel):
    """PostgreSQL connection settings."""

    host: str
    port: int
    user: str
    password: str
    name: str

    @property
    def connection_string(self) -> str:
        """Build PostgreSQL connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class LLMConfig(BaseModel):
    """LLM provider settings."""

    model_boss: str


class WorkerConfig(BaseModel):
    """Worker tool settings."""

    tool_type: Literal["claude_code", "openhands"]
    tool_model: str
    tool_timeout: int


class OrchestrationConfig(BaseModel):
    """Execution behavior settings."""

    class BudgetConfig(BaseModel):
        """Cost and token limits."""

        max_total_cost_usd: float = Field(ge=0.0)
        max_tokens_per_agent: int = Field(gt=0)
        cost_warning_threshold: float = Field(ge=0.0, le=1.0)
        cost_tracking_enabled: bool

    class LimitsConfig(BaseModel):
        """Agent hierarchy and concurrency limits."""

        max_depth: int
        max_children_per_node: int
        max_total_agents: int
        max_concurrent_workers: int
        llm_rate_limit_rpm: int

        def is_depth_limited(self) -> bool:
            """Check if depth limit is enabled."""
            return self.max_depth > 0

        def is_children_limited(self) -> bool:
            """Check if children-per-node limit is enabled."""
            return self.max_children_per_node > 0

        def is_agents_limited(self) -> bool:
            """Check if total agents limit is enabled."""
            return self.max_total_agents > 0

        def is_workers_limited(self) -> bool:
            """Check if concurrent workers limit is enabled."""
            return self.max_concurrent_workers > 0

    max_retries: int = Field(ge=0, le=10)
    retry_delay: float = Field(ge=0.0)
    poll_interval: float = Field(ge=0.1)
    llm_timeout: float = Field(gt=0.0)
    worker_timeout: float = Field(gt=0.0)
    default_task_complexity_threshold: int = Field(ge=1, le=10)
    worker_shortcut_probability: float = Field(ge=0.0, le=1.0, default=0.3)
    budget_threshold_ratio: float = Field(ge=0.0, le=1.0, default=0.02)

    budget: BudgetConfig
    limits: LimitsConfig


class OutputConfig(BaseModel):
    """UI and output settings."""

    verbose: bool
    show_progress: bool
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    directory: str


class SecurityConfig(BaseModel):
    """Security benchmark generation settings."""

    enabled: bool = True
    auto_detect: bool = True  # Auto-detect security tasks from keywords
    default_model_poc: str = "o3"  # Model for PoC generation
    default_model_patch: str = "claude-sonnet-4-5-20250514"  # Model for patch generation
    default_model_validation: str = "o3-mini"  # Model for validation
    poc_temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    patch_temperature: float = Field(default=0.5, ge=0.0, le=2.0)
    validation_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_poc_attempts: int = Field(default=3, ge=1, le=10)
    max_patch_attempts: int = Field(default=3, ge=1, le=10)
    docker_timeout: int = Field(default=300, gt=0)  # Seconds for Docker operations
    sanitizer_flags: str = "-fsanitize=address,undefined -g"


class Settings(BaseSettings):
    """Root config: secrets from env, everything else from YAML."""

    model_config = SettingsConfigDict(extra="ignore")

    database: DatabaseConfig
    llm: LLMConfig
    worker: WorkerConfig
    orchestration: OrchestrationConfig
    output: OutputConfig
    security: SecurityConfig = SecurityConfig()  # Optional with defaults

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> "Settings":
        """Build Settings from config dict, injecting env vars."""
        db_config = config.get("database", {})

        postgres_password = os.getenv("POSTGRES_PASSWORD")
        if not postgres_password:
            raise ValueError("POSTGRES_PASSWORD environment variable is required")
        db_config["password"] = postgres_password

        postgres_host_override = os.getenv("POSTGRES_HOST")
        if postgres_host_override:
            db_config["host"] = postgres_host_override

        # Security config is optional with defaults
        security_config = config.get("security", {})

        return cls(
            database=DatabaseConfig(**db_config),
            llm=LLMConfig(**config.get("llm", {})),
            worker=WorkerConfig(**config.get("worker", {})),
            orchestration=OrchestrationConfig(**config.get("orchestration", {})),
            output=OutputConfig(**config.get("output", {})),
            security=SecurityConfig(**security_config) if security_config else SecurityConfig(),
        )

    @classmethod
    def load(cls, env: str | None = None) -> "Settings":
        """Load settings: YAML config + env secrets."""
        config = _load_yaml_hierarchy(env)
        return cls._build_from_config(config)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load from specific YAML file (for testing)."""
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with config_file.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        return cls._build_from_config(config)
