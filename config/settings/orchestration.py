"""Execution-behavior settings: topology, concurrency, retries, orchestration flags."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .toolsets import ToolCallingConfig


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

    treatment_version: str | None = Field(
        default=None,
        description="Frozen experimental treatment identifier persisted on RunStarted.",
    )

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
    procedural_dispatch: bool = Field(
        default=False,
        description=(
            "Bind the domain plugin's procedure executor so registry-matched worker "
            "tasks run host-side with zero LLM turns (failed procedures escalate to "
            "an agentic retry). Off => the null executor; behavior byte-identical."
        ),
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
            "Default keeps the legacy order (per-phase domain display and mindset "
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
    scoped_worker_context: bool = Field(
        default=False,
        description="Assemble consumer-scoped latest-revision packets instead of a run-global block.",
    )
    source_context_token_budget: int = Field(
        default=16_000,
        gt=0,
        description="Maximum source tokens in one scoped worker context packet.",
    )
    metadata_context_token_budget: int = Field(
        default=4_000,
        gt=0,
        description=(
            "Maximum tokens for a scoped packet's metadata/evidence index segment "
            "(the provided-files index and its omission log). Overflow entries are "
            "recorded as a single metadata_budget omission instead of overflowing."
        ),
    )
    workspace_listing_dirs: list[str] | None = Field(
        default=None,
        description=(
            "Whitelist of top-level workspace directories to include in the "
            "<workspace> file listing of worker prompts (e.g. ['artifacts']). "
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
