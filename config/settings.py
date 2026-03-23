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


class ReconRoleConfig(BaseModel):
    """Per-role recon policy defaults."""

    enabled: bool = True
    max_iterations: int = 5
    result_char_limit: int = 6_000
    allowed_tools: list[str] | None = None


class ReconConfig(BaseModel):
    """Per-role + per-domain recon policy config.

    Shape::

        recon:
          default:
            pending: {enabled: true, max_iterations: 5}
            manager: {enabled: true, max_iterations: 5}
            boss: {enabled: false}
          domains:
            secbench:
              manager: {max_iterations: 3, allowed_tools: [...]}
    """

    default: dict[str, ReconRoleConfig] = Field(default_factory=dict)
    domains: dict[str, dict[str, ReconRoleConfig]] = Field(default_factory=dict)

    def to_raw_dict(self) -> dict[str, Any]:
        """Convert to the raw dict format consumed by AgentOrchestrator."""
        return self.model_dump(mode="python")


class OrchestrationConfig(BaseModel):
    """Execution behavior settings."""

    class LimitsConfig(BaseModel):
        """Agent hierarchy and concurrency limits."""

        max_depth: int
        max_children_per_node: int
        max_total_agents: int
        max_concurrent_workers: int
        llm_rate_limit_rpm: int
        max_concurrent_llm_calls: int = 5
        llm_jitter_max_ms: int = 500
        max_recon_iterations: int = 10

        # Context condensation for recon tool-calling loop
        recon_result_char_limit: int = 6_000
        recon_condense_after_iteration: int = 2
        recon_token_budget: int = 80_000

        def is_depth_limited(self) -> bool:
            return self.max_depth > 0

        def is_children_limited(self) -> bool:
            return self.max_children_per_node > 0

        def is_agents_limited(self) -> bool:
            return self.max_total_agents > 0

        def is_workers_limited(self) -> bool:
            return self.max_concurrent_workers > 0

        def is_llm_limited(self) -> bool:
            return self.max_concurrent_llm_calls > 0

    class RetryConfig(BaseModel):
        """Retry and auto-healing settings (Phase 4)."""

        model_escalation_chain: list[str] = Field(
            default_factory=list,
            description="Models to try on failure, in order",
        )
        retry_budget_fraction: float = Field(
            default=0.3,
            ge=0.0,
            le=1.0,
            description="Fraction of remaining budget available for retries",
        )
        circuit_breaker_threshold: int = Field(
            default=3,
            ge=1,
            description="Consecutive failures before tripping circuit breaker",
        )
        circuit_breaker_reset_seconds: int = Field(
            default=300,
            ge=0,
            description="Seconds before circuit breaker resets",
        )

    max_retries: int = Field(ge=0, le=10)
    poll_interval: float = Field(ge=0.01)
    decomposition_strategy: str = Field(
        default="recursive",
        description="How tasks are decomposed: recursive (default) or flat",
    )
    global_budget_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Global budget limit in USD (0 = unlimited)",
    )

    limits: LimitsConfig
    retry: RetryConfig = RetryConfig()
    recon: ReconConfig = ReconConfig()


class OutputConfig(BaseModel):
    """UI and output settings."""

    verbose: bool
    show_progress: bool
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    directory: str


class SecurityConfig(BaseModel):
    """SEC-bench container settings."""

    enabled: bool = True  # Uses worker.timeout for secb commands
    tools: list[str] = Field(
        default_factory=lambda: ["valgrind", "klee"],
        description="Security analysis tools to enable in SEC-bench containers",
    )


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
