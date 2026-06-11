"""Configuration via Pydantic Settings with phase-specific YAML support."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
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

    model_config = {"extra": "forbid"}

    host: str
    port: int
    user: str
    password: str
    name: str
    # asyncpg pool sizing (G.1). Default matches the historical asyncpg
    # defaults of (min=10, max=10) so unchanged deployments behave the
    # same; raise pool_max to widen the connection pool under load.
    pool_min: int = Field(default=10, ge=1, le=100)
    pool_max: int = Field(default=10, ge=1, le=100)

    @property
    def connection_string(self) -> str:
        """Build PostgreSQL connection string."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class BossConfig(BaseModel):
    """Boss agent LLM settings."""

    model_config = {"extra": "forbid"}

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description=(
            "Optional LiteLLM api_base override (e.g. https://ollama.com for Ollama Cloud)."
        ),
    )


class ManagerConfig(BaseModel):
    """Manager agent LLM settings."""

    model_config = {"extra": "forbid"}

    model: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description=(
            "Optional LiteLLM api_base override (e.g. https://ollama.com for Ollama Cloud)."
        ),
    )


class ClaudeCodeParams(BaseModel):
    """Tool-specific params for the Claude Code worker backend."""

    model_config = {"extra": "forbid"}

    output_format: Literal["stream-json", "text"] = "stream-json"
    include_partial_messages: bool = True
    max_turns: int = Field(default=40, gt=0)
    use_global_config: bool = False


class OpenHandsParams(BaseModel):
    """OpenHands-specific worker settings."""

    model_config = {"extra": "forbid"}

    mcp_tools: list[str] = Field(default_factory=list)
    enable_subagents: bool = Field(
        default=False,
        description=(
            "Expose OpenHands' native task-delegation tool so the worker can "
            "spawn a bounded two-level tree of child agents (N2 baseline)."
        ),
    )
    run_scoped_prompt_cache_key: bool = Field(
        default=False,
        description=(
            "Pin the SDK's OpenAI prompt_cache_key to the run root id so every "
            "worker in a run shares one prefix-cache shard. The SDK default pins "
            "per-conversation, which puts each worker on its own shard and "
            "defeats cross-worker prefix-cache reuse."
        ),
    )


class GoogleAdkParams(BaseModel):
    """Tool-specific params for the Google ADK worker backend.

    Placeholder; expand when the adapter consumes structured params.
    """

    model_config = {"extra": "forbid"}


class WorkerToolParams(BaseModel):
    """Tagged union by tool name. Only the slot matching ``worker.tool`` is populated;
    others are ``None``. The ``Settings`` model validator enforces this.
    """

    model_config = {"extra": "forbid"}

    claude_code: ClaudeCodeParams | None = None
    openhands: OpenHandsParams | None = None
    google_adk: GoogleAdkParams | None = None


ReasoningEffort = Literal["low", "medium", "high", "xhigh", "none"]


class WorkerConfig(BaseModel):
    """Worker tool settings."""

    model_config = {"extra": "forbid"}

    model: str
    tool: Literal["claude_code", "openhands", "google_adk"]
    allowed_tools: list[str] = Field(default_factory=lambda: ["*"])
    disallowed_tools: list[str] = Field(default_factory=list)
    timeout: int = Field(default=300, gt=0)
    max_iterations_per_run: int = Field(
        default=20,
        gt=0,
        description="Per-run iteration cap for worker tools that support it.",
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Optional base URL for the worker LLM (e.g. https://ollama.com for Ollama Cloud)."
        ),
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description=(
            "Reasoning effort for worker LLM calls. None keeps the worker SDK "
            "default (OpenHands defaults to 'high', the dominant completion-token "
            "cost for reasoning models)."
        ),
    )
    reasoning_effort_overrides: dict[str, ReasoningEffort] = Field(
        default_factory=dict,
        description=(
            "Task-prefix → reasoning-effort overrides; the first key the worker's "
            "task description starts with wins (e.g. '[Exploiter]': medium). "
            "Falls back to reasoning_effort when nothing matches."
        ),
    )
    tool_params: WorkerToolParams = Field(default_factory=WorkerToolParams)


class ApiWorkerConfig(BaseModel):
    """Worker settings the query API can expose without requiring run models."""

    model_config = {"extra": "ignore"}

    tool: Literal["claude_code", "openhands", "google_adk"]
    timeout: int = Field(default=300, gt=0)
    max_iterations_per_run: int = Field(default=20, gt=0)


class FormatRepairerConfig(BaseModel):
    """Fallback LLM-backed output-format repairer settings.

    Used when the deterministic ``raw_decode`` / ``_repair_json`` chain
    fails on output from less-disciplined models (qwen3, deepseek, GLM,
    unknown). The repair model must be configured by YAML; there is no
    source-code model fallback.

    Distinct from the security-domain "Fixer" agent role — this repairs
    output format, never source code.
    """

    model_config = {"extra": "forbid"}

    # Disabled by default. Enable when running on models prone to malformed
    # output (qwen3, qwen3.5, deepseek, GLM, …). Adds a dependency on the
    # configured repair model and a one-shot LLM call per parse failure.
    # GPT/Claude paths are unaffected when enabled.
    enabled: bool = False
    model: str = Field(min_length=1)
    # Default 16000 to match boss/manager.max_tokens — a repaired output
    # cannot need more space than the source model could have produced.
    # Empirical max from prior runs: ~5000 tokens; p99 ~3500.
    max_tokens: int = Field(default=16000, gt=0, le=100000)
    api_base: str | None = Field(
        default=None,
        description="Optional LiteLLM api_base override for the repairer.",
    )
    # Bound concurrent repair calls so a parse-failure storm cannot
    # amplify into N simultaneous LLM requests against the repair
    # endpoint. Sized to host capacity (Ollama Cloud or local Ollama),
    # not to source-model concurrency.
    max_concurrent: int = Field(default=3, ge=1, le=32)


class ToolsetPolicyConfig(BaseModel):
    """Per-toolset enablement and allowed-tool overrides."""

    model_config = {"extra": "forbid"}

    enabled: bool = True
    allowed_tools: list[str] | None = None


class ToolsetRoleConfig(BaseModel):
    """Per-role tool-calling policy defaults."""

    model_config = {"extra": "forbid"}

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
            <domain>:
              manager:
                max_iterations: 3
                toolsets:
                  recon: {allowed_tools: [...]}
    """

    model_config = {"extra": "forbid"}

    default: dict[str, ToolsetRoleConfig] = Field(default_factory=dict)
    domains: dict[str, dict[str, ToolsetRoleConfig]] = Field(default_factory=dict)

    def to_raw_dict(self) -> dict[str, Any]:
        """Convert to the normalized dict format consumed by the resolver."""
        return self.model_dump(mode="python")


class TopologyConfig(BaseModel):
    """Agent hierarchy shape limits."""

    model_config = {"extra": "forbid"}

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

    model_config = {"extra": "forbid"}

    max_concurrent_workers: int
    max_concurrent_llm_calls: int = 5
    llm_jitter_max_ms: int = 500
    # Hard cap on a single agent step (assess/decompose/judge/dispatch). Stops
    # the orchestrator from blocking forever inside LiteLLM retry storms.
    max_agent_step_seconds: float = 900.0

    def is_workers_limited(self) -> bool:
        return self.max_concurrent_workers > 0

    def is_llm_limited(self) -> bool:
        return self.max_concurrent_llm_calls > 0


class ToolCallingConfig(BaseModel):
    """Tool-calling loop defaults and per-role policies."""

    model_config = {"extra": "forbid"}

    max_iterations: int = 10
    result_char_limit: int = 6_000
    condense_after_iteration: int = 2
    token_budget: int = 80_000
    policies: ToolsetConfig = Field(default_factory=ToolsetConfig)


class RetryConfig(BaseModel):
    """Retry and auto-healing settings."""

    model_config = {"extra": "forbid"}

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


class OrchestrationConfig(BaseModel):
    """Execution behavior settings."""

    model_config = {"extra": "forbid"}

    mode: Literal["hierarchical", "flat"] = Field(
        default="hierarchical",
        description="Top-level execution shape: hierarchical (BOSS->managers->workers) or flat.",
    )
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
    skip_judge: bool = Field(
        default=False,
        description="Skip the LLM judge stage of verification (stages 1-3 still run).",
    )
    shared_worker_session: bool = Field(
        default=False,
        description=(
            "When True, source files a worker's view tool returns are captured as "
            "SourceFile events and re-injected verbatim into later workers' prompts "
            "(the shared code-prefix block). Workers always run their own fresh "
            "OpenHands conversation (raw conversation reuse overflowed the worker "
            "context window), and a run's workers always share one container "
            "regardless of this flag. Each role keeps its own AgentSession "
            "aggregate, so per-role events and cost attribution are preserved."
        ),
    )
    shared_code_prefix_first: bool = Field(
        default=False,
        description=(
            "Place the shared code block BEFORE the per-phase domain content in "
            "worker prompts, so the run-global block sits in the byte region all "
            "branches share and cross-branch prefix-cache hits become possible. "
            "Default keeps the legacy order (per-phase CVE display and mindset "
            "first), which forks the cache per branch."
        ),
    )
    shared_code_index: bool = Field(
        default=False,
        description=(
            "Render a compact <provided_files_index> (path, revision, freshness) "
            "in the volatile prompt tail near the worker's <task>. Pure data, no "
            "imperative text — raises the salience of already-provided files so "
            "workers are less likely to re-read them via tools."
        ),
    )
    shared_code_skip_dir_listings: bool = Field(
        default=False,
        description=(
            "Skip capturing directory-listing view results into the shared code "
            "block. Directory snapshots go stale immediately and duplicate the "
            "workspace listing."
        ),
    )
    shared_code_render_mode: Literal["append_only", "latest_only"] = Field(
        default="append_only",
        description=(
            "append_only keeps every file revision in the shared block (byte-"
            "stable prefix; stale revisions ride along at cache-read price). "
            "latest_only renders one entry per path with its newest content — "
            "smaller block, but a re-viewed file rewrites the block mid-run and "
            "busts the prefix cache from that point for later-spawned workers."
        ),
    )
    workspace_listing_dirs: list[str] | None = Field(
        default=None,
        description=(
            "Whitelist of top-level workspace directories to include in the "
            "<workspace> file listing of worker prompts (e.g. ['testcase']). "
            "None keeps the legacy behavior: list everything except src/. "
            "Build trees (work/*-build, CMakeFiles) made the legacy listing "
            "balloon to ~50KB per prompt turn."
        ),
    )
    workspace_listing_max_entries: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Cap on workspace-listing entries; overflow is summarized as "
            "'… (+N more files)'. None = unlimited (legacy)."
        ),
    )
    verification_max_retries: int = Field(
        default=2,
        ge=0,
        le=10,
        description=(
            "Verification-failure retries per worker. Each retry re-runs a full "
            "worker conversation (up to worker.max_iterations_per_run LLM calls), "
            "so this is a direct cost multiplier on failing workers."
        ),
    )
    capture_recon_reads: bool = Field(
        default=False,
        description=(
            "Persist a manager's recon read_file results into the shared code "
            "block (as SourceFileObserved on the manager's aggregate), so the "
            "run carries them to downstream workers — read once, propagate. "
            "Requires shared_worker_session. Off => recon reads are not captured."
        ),
    )
    share_boss_recon: bool = Field(
        default=False,
        description=(
            "Inject the boss's recon read_file results into MANAGER decomposition "
            "prompts so managers inherit the boss's reads instead of re-reading. "
            "Orchestration-tier only — the block is held in memory and never "
            "reaches the worker-consumed shared block, so worker cost is "
            "unaffected. Off => no boss→manager propagation."
        ),
    )

    topology: TopologyConfig
    concurrency: ConcurrencyConfig
    tool_calling: ToolCallingConfig = Field(default_factory=ToolCallingConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class OutputConfig(BaseModel):
    """UI and output settings."""

    model_config = {"extra": "forbid"}

    verbose: bool
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    directory: str


class SecurityConfig(BaseModel):
    """SEC-bench container settings."""

    model_config = {"extra": "forbid"}

    enabled: bool = True  # Uses worker.timeout for secb commands
    tools: list[str] = Field(
        default_factory=lambda: ["valgrind"],
        description="Security analysis tools to enable in SEC-bench containers",
    )
    worker_network_mode: Literal["bridge", "host"] = "host"
    worker_docker_timeout_seconds: int = Field(default=300, ge=1, le=3600)
    tools_image_registry: str = Field(
        default="",
        description=(
            "Registry namespace hosting prebuilt secb-tools images "
            "(e.g. 'cheshire0814' or 'ghcr.io/org'). When set, a run that cannot "
            "find secb-tools:<tag> locally pulls <registry>/secb-tools:<tag> "
            "and retags it, so fresh machines run without building. Empty "
            "disables the fallback (local build only)."
        ),
    )


class CorsConfig(BaseModel):
    """CORS (Cross-Origin Resource Sharing) settings."""

    model_config = {"extra": "forbid"}

    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    allowed_methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    )
    allowed_headers: list[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True


_WORKER_TOOL_PARAM_TYPES: dict[str, type[BaseModel]] = {
    "claude_code": ClaudeCodeParams,
    "openhands": OpenHandsParams,
    "google_adk": GoogleAdkParams,
}


def _inject_env_database(config: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(config)

    db_config = dict(merged.get("database", {}))
    postgres_password = os.getenv("POSTGRES_PASSWORD")
    if not postgres_password:
        raise ValueError("POSTGRES_PASSWORD environment variable is required")
    db_config["password"] = postgres_password
    if os.getenv("POSTGRES_HOST"):
        db_config["host"] = os.getenv("POSTGRES_HOST")
    postgres_port = os.getenv("POSTGRES_PORT")
    if postgres_port:
        db_config["port"] = int(postgres_port)
    if os.getenv("POSTGRES_USER"):
        db_config["user"] = os.getenv("POSTGRES_USER")
    if os.getenv("POSTGRES_DB"):
        db_config["name"] = os.getenv("POSTGRES_DB")
    merged["database"] = db_config
    return merged


class ApiSettings(BaseSettings):
    """Query API config that intentionally does not require experiment models."""

    model_config = SettingsConfigDict(extra="ignore")

    database: DatabaseConfig
    worker: ApiWorkerConfig
    orchestration: OrchestrationConfig
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> ApiSettings:
        return cls.model_validate(_inject_env_database(config))

    @classmethod
    def load(cls, env: str | None = None) -> ApiSettings:
        config = _load_yaml_hierarchy(env)
        return cls._build_from_config(config)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> ApiSettings:
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        from config._paths import get_repo_root
        from config.overlay import resolve_overlay

        config = resolve_overlay(config_file, repo_root=get_repo_root())
        return cls._build_from_config(config)


class Settings(BaseSettings):
    """Root config: secrets from env, everything else from YAML."""

    model_config = SettingsConfigDict(extra="forbid")

    database: DatabaseConfig
    boss: BossConfig
    manager: ManagerConfig
    worker: WorkerConfig
    format_repairer: FormatRepairerConfig
    orchestration: OrchestrationConfig
    output: OutputConfig
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)

    @model_validator(mode="after")
    def _validate_worker_tool_params(self) -> Settings:
        """Tagged-union check: ``worker.tool_params.<tool>`` must be populated
        when ``worker.tool`` selects that tool, and no other slot may carry a
        payload (catches operator typos like ``tool: openhands`` paired with
        a stray ``claude_code:`` block).
        """
        active = self.worker.tool
        slot = getattr(self.worker.tool_params, active)
        if slot is None:
            raise ValueError(
                f"worker.tool_params.{active} must be populated when worker.tool={active!r}"
            )
        populated_others = [
            name
            for name in _WORKER_TOOL_PARAM_TYPES
            if name != active and getattr(self.worker.tool_params, name) is not None
        ]
        if populated_others:
            raise ValueError(
                f"worker.tool_params has populated slots for inactive tools: "
                f"{populated_others} (active tool is {active!r}). "
                f"Remove the unused slots or change worker.tool."
            )
        return self

    @classmethod
    def _build_from_config(cls, config: dict[str, Any]) -> Settings:
        """Build Settings from config dict, injecting env vars."""
        merged = _inject_env_database(config)

        merged["worker"] = _populate_default_tool_params(dict(merged.get("worker", {})))

        return cls.model_validate(merged)

    @classmethod
    def load(cls, env: str | None = None) -> Settings:
        """Load settings: YAML config + env secrets."""
        config = _load_yaml_hierarchy(env)
        return cls._build_from_config(config)

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> Settings:
        """Load from a specific YAML file (for tests) or an overlay file.

        Overlay support: if the YAML declares ``extends:`` + ``overrides:``,
        they are resolved recursively against the base before validation.
        """
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        from config._paths import get_repo_root
        from config.overlay import resolve_overlay

        config = resolve_overlay(config_file, repo_root=get_repo_root())
        return cls._build_from_config(config)


def _populate_default_tool_params(worker_raw: dict[str, Any]) -> dict[str, Any]:
    """If ``worker.tool_params`` is absent for the active tool, fill the
    matching slot with that tool's default params.

    An explicit ``null`` left in YAML by the operator (e.g. via a
    ``tool_params: {openhands: null}`` override) is preserved so the
    ``Settings`` model validator can reject the misconfiguration loudly.
    """
    tool = worker_raw.get("tool")
    if tool not in _WORKER_TOOL_PARAM_TYPES:
        # Let WorkerConfig's own Literal validation report unknown tools.
        return worker_raw
    raw_params = worker_raw.get("tool_params")
    if raw_params is None and "tool_params" not in worker_raw:
        worker_raw["tool_params"] = {tool: _WORKER_TOOL_PARAM_TYPES[tool]().model_dump()}
        return worker_raw
    if isinstance(raw_params, dict) and tool not in raw_params:
        raw_params = dict(raw_params)
        raw_params[tool] = _WORKER_TOOL_PARAM_TYPES[tool]().model_dump()
        worker_raw["tool_params"] = raw_params
    return worker_raw
