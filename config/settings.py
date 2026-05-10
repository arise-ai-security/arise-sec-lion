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
    api_base: str | None = Field(
        default=None,
        description="Optional LiteLLM api_base override (e.g. https://ollama.com for Ollama Cloud).",
    )


class ManagerConfig(BaseModel):
    """Manager agent LLM settings."""

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description="Optional LiteLLM api_base override (e.g. https://ollama.com for Ollama Cloud).",
    )


class WorkerConfig(BaseModel):
    """Worker tool settings."""

    model: str
    tool: Literal["claude_code", "openhands", "google_adk"]
    timeout: int = Field(default=300, gt=0)
    max_iterations_per_run: int = Field(
        default=20,
        gt=0,
        description="Per-run iteration cap for worker tools that support it.",
    )
    base_url: str | None = Field(
        default=None,
        description="Optional base URL for the worker LLM (e.g. https://ollama.com for Ollama Cloud).",
    )


class FormatRepairerConfig(BaseModel):
    """Fallback LLM-backed output-format repairer settings.

    Used when the deterministic ``raw_decode`` / ``_repair_json`` chain
    fails on output from less-disciplined models (qwen3, deepseek, GLM,
    unknown). Default model is the one that most commonly produces
    malformed output via Ollama Cloud — it knows what it meant to emit.

    Distinct from the security-domain "Fixer" agent role — this repairs
    output format, never source code.
    """

    # Disabled by default. Enable when running on models prone to malformed
    # output (qwen3, qwen3.5, deepseek, GLM, …). Adds a dependency on the
    # configured repair model (default: Ollama Cloud) and a one-shot LLM
    # call per parse failure. GPT/Claude paths are unaffected when enabled.
    enabled: bool = False
    # Default to qwen3-coder:480b-cloud (Ollama Cloud, coder-tuned, no
    # thinking-on-by-default). Reasoning:
    # - Coder-specialised → reliable structured-JSON output.
    # - No <think> tags → won't reintroduce the failure mode the
    #   qwen3.5 reasoning model causes (the very thing we're repairing).
    # - Different model family from typical boss/manager/worker (qwen3.5
    #   or gpt-5.x) → independence from the source-of-the-bug model.
    # - Only fires on parse failure (0–3× per run worst-case), so the
    #   accuracy/speed tradeoff favours accuracy over the smaller
    #   qwen3-coder-next:cloud preview.
    model: str = "ollama_chat/qwen3-coder:480b-cloud"
    # Default 16000 to match boss/manager.max_tokens — a repaired output
    # cannot need more space than the source model could have produced.
    # Empirical max from prior runs: ~5000 tokens; p99 ~3500.
    max_tokens: int = Field(default=16000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description="Optional LiteLLM api_base override for the repairer.",
    )


class ToolsetPolicyConfig(BaseModel):
    """Per-toolset enablement and allowed-tool overrides."""

    enabled: bool = True
    allowed_tools: list[str] | None = None


class ToolsetRoleConfig(BaseModel):
    """Per-role tool-calling policy defaults."""

    max_iterations: int = 5
    result_char_limit: int = 6_000
    toolsets: dict[str, ToolsetPolicyConfig] = Field(default_factory=dict)


class ToolsetConfig(BaseModel):
    """Per-role + per-domain toolset policy config.

    Shape::

        toolsets:
          default:
            pending:
              max_iterations: 5
              toolsets:
                recon: {enabled: true}
            manager:
              max_iterations: 5
              toolsets:
                recon: {enabled: true}
            boss:
              toolsets:
                recon: {enabled: false}
          domains:
            secbench:
              manager:
                max_iterations: 3
                toolsets:
                  recon: {allowed_tools: [...]}
    """

    default: dict[str, ToolsetRoleConfig] = Field(default_factory=dict)
    domains: dict[str, dict[str, ToolsetRoleConfig]] = Field(default_factory=dict)

    def to_raw_dict(self) -> dict[str, Any]:
        """Convert to the normalized dict format consumed by the resolver."""
        return self.model_dump(mode="python")


class TopologyConfig(BaseModel):
    """Agent hierarchy shape limits."""

    max_depth: int
    max_children_per_node: int
    max_total_agents: int

    def is_depth_limited(self) -> bool:
        return self.max_depth > 0

    def is_children_limited(self) -> bool:
        return self.max_children_per_node > 0

    def is_agents_limited(self) -> bool:
        return self.max_total_agents > 0


class ConcurrencyConfig(BaseModel):
    """Parallelism and rate-limiting settings."""

    max_concurrent_workers: int
    max_concurrent_llm_calls: int = 5
    llm_jitter_max_ms: int = 500

    def is_workers_limited(self) -> bool:
        return self.max_concurrent_workers > 0

    def is_llm_limited(self) -> bool:
        return self.max_concurrent_llm_calls > 0


class ToolCallingConfig(BaseModel):
    """Tool-calling loop defaults and per-role policies."""

    max_iterations: int = 10
    result_char_limit: int = 6_000
    condense_after_iteration: int = 2
    token_budget: int = 80_000
    policies: ToolsetConfig = Field(default_factory=ToolsetConfig)


class RetryConfig(BaseModel):
    """Retry and auto-healing settings."""

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
    no_progress_max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Max retries for workers that emit zero thoughts after execution starts. "
            "Independent of model escalation chain."
        ),
    )
    step_timeout_max_retries: int = Field(
        default=2,
        ge=0,
        description="Max retries for worker step-timeout failures.",
    )
    silent_worker_max_retries: int = Field(
        default=2,
        ge=0,
        description="Max retries for worker silence watchdog failures.",
    )
    pending_assessment_max_retries: int = Field(
        default=2,
        ge=0,
        description="Max retries for stalled pending-assessment failures.",
    )


class OrchestrationConfig(BaseModel):
    """Execution behavior settings."""

    max_retries: int = Field(ge=0, le=10)
    poll_interval: float = Field(ge=0.01)
    max_run_duration_seconds: float = Field(
        default=1800,
        gt=0,
        description="Hard cap for the overall system loop runtime.",
    )
    max_redecompositions: int = Field(
        default=2,
        ge=0,
        description="Maximum infeasibility-driven redecompositions per parent.",
    )
    decomposition_strategy: str = Field(
        default="recursive",
        description="How tasks are decomposed: recursive (default) or flat",
    )
    global_budget_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Global budget limit in USD (0 = unlimited)",
    )
    skip_judge: bool = Field(
        default=False,
        description="Skip the LLM judge stage of verification (stages 1-3 still run).",
    )

    topology: TopologyConfig
    concurrency: ConcurrencyConfig
    tool_calling: ToolCallingConfig = Field(default_factory=ToolCallingConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)


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
    format_repairer: FormatRepairerConfig = FormatRepairerConfig()  # Optional with defaults
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

        format_repairer_config = config.get("format_repairer", {})

        return cls(
            database=DatabaseConfig(**db_config),
            boss=BossConfig(**config.get("boss", {})),
            manager=ManagerConfig(**config.get("manager", {})),
            worker=WorkerConfig(**config.get("worker", {})),
            format_repairer=(
                FormatRepairerConfig(**format_repairer_config)
                if format_repairer_config
                else FormatRepairerConfig()
            ),
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
