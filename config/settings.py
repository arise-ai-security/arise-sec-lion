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


class BossConfig(BaseModel):
    """Boss agent LLM settings."""

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)


class ManagerConfig(BaseModel):
    """Manager agent LLM settings."""

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)


class WorkerConfig(BaseModel):
    """Worker tool settings."""

    model: str
    tool: Literal["claude_code", "openhands", "google_adk"]
    timeout: int = Field(default=300, gt=0)
    max_tool_calls: int = Field(default=-1)  # -1 = unlimited, positive = hard limit


class OrchestrationConfig(BaseModel):
    """Execution behavior settings.

    Note: Budget tracking will be added via SharedExecutionContext (context-passing feature).
    """

    class LimitsConfig(BaseModel):
        """Agent hierarchy and concurrency limits."""

        max_depth: int
        max_children_per_node: int
        max_total_agents: int
        max_concurrent_workers: int
        llm_rate_limit_rpm: int
        max_concurrent_llm_calls: int = 5  # Concurrent LLM API calls (-1 = unlimited)
        llm_jitter_max_ms: int = 500  # Max jitter before LLM calls in ms (0 = disabled)

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

        def is_llm_limited(self) -> bool:
            """Check if concurrent LLM calls limit is enabled."""
            return self.max_concurrent_llm_calls > 0

    max_retries: int = Field(ge=0, le=10)
    retry_delay: float = Field(ge=0.0)
    poll_interval: float = Field(ge=0.01)  # Minimum 10ms
    llm_timeout: float = Field(gt=0.0)
    worker_timeout: float = Field(gt=0.0)
    default_task_complexity_threshold: int = Field(ge=1, le=10)

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
    default_model_poc: str = "gpt-4o"  # Model for PoC generation
    default_model_patch: str = "claude-3-5-sonnet-20241022"  # Model for patch generation
    default_model_validation: str = "gpt-4o-mini"  # Model for validation
    poc_temperature: float = Field(default=0.6, ge=0.0, le=2.0)
    patch_temperature: float = Field(default=0.5, ge=0.0, le=2.0)
    validation_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_poc_attempts: int = Field(default=3, ge=1, le=10)
    max_patch_attempts: int = Field(default=3, ge=1, le=10)
    docker_timeout: int = Field(default=300, gt=0)  # Seconds for Docker operations
    sanitizer_flags: str = "-fsanitize=address,undefined -g"


class CorsConfig(BaseModel):
    """CORS (Cross-Origin Resource Sharing) settings."""

    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    allowed_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )
    allowed_headers: list[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True


class Settings(BaseSettings):
    """Root config: secrets from env, everything else from YAML."""

    model_config = SettingsConfigDict(extra="ignore")

    database: DatabaseConfig
    boss: BossConfig
    manager: ManagerConfig
    worker: WorkerConfig
    orchestration: OrchestrationConfig
    output: OutputConfig
    security: SecurityConfig = SecurityConfig()  # Optional with defaults
    cors: CorsConfig = CorsConfig()  # Optional with defaults

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> "Settings":
        """Build Settings from config dict, injecting env vars."""
        db_config = config.get("database", {})

        # Password is required from environment (secrets)
        postgres_password = os.getenv("POSTGRES_PASSWORD")
        if not postgres_password:
            raise ValueError("POSTGRES_PASSWORD environment variable is required")
        db_config["password"] = postgres_password

        # Allow env overrides for all database settings (useful for dev/cloud DBs)
        if os.getenv("POSTGRES_HOST"):
            db_config["host"] = os.getenv("POSTGRES_HOST")
        if os.getenv("POSTGRES_PORT"):
            db_config["port"] = int(os.getenv("POSTGRES_PORT"))
        if os.getenv("POSTGRES_USER"):
            db_config["user"] = os.getenv("POSTGRES_USER")
        if os.getenv("POSTGRES_DB"):
            db_config["name"] = os.getenv("POSTGRES_DB")

        # Security config is optional with defaults
        security_config = config.get("security", {})

        # CORS config is optional with defaults
        cors_config = config.get("cors", {})

        return cls(
            database=DatabaseConfig(**db_config),
            boss=BossConfig(**config.get("boss", {})),
            manager=ManagerConfig(**config.get("manager", {})),
            worker=WorkerConfig(**config.get("worker", {})),
            orchestration=OrchestrationConfig(**config.get("orchestration", {})),
            output=OutputConfig(**config.get("output", {})),
            security=SecurityConfig(**security_config) if security_config else SecurityConfig(),
            cors=CorsConfig(**cors_config) if cors_config else CorsConfig(),
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
