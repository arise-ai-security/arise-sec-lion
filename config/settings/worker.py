"""Worker tool settings and the per-tool tagged-union params."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


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
    mcp_tool_timeout_seconds: int = Field(
        default=600,
        ge=1,
        le=3600,
        description=(
            "Per-call timeout (seconds) for OpenHands MCP tools (e.g. "
            "shell_in_container). Applied to the SDK so all workers in a run "
            "share one consistent shell timeout."
        ),
    )
    enable_subagents: bool = Field(
        default=False,
        description=(
            "Expose OpenHands' native task-delegation tool so the worker can "
            "spawn a bounded two-level tree of child agents."
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
    model_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Task-prefix → worker-model overrides; the first key the worker's task "
            "description starts with wins (e.g. '[Analysis]': model-name). "
            "Falls back to model when nothing matches."
        ),
    )
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
            "task description starts with wins (e.g. '[Analysis]': medium). "
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


_WORKER_TOOL_PARAM_TYPES: dict[str, type[BaseModel]] = {
    "claude_code": ClaudeCodeParams,
    "openhands": OpenHandsParams,
    "google_adk": GoogleAdkParams,
}


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
