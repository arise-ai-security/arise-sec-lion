"""OpenHands SDK adapter: use the SDK conversation directly for task execution."""

import asyncio
import concurrent.futures
import logging
import os
import re
import signal
import subprocess
import threading
import time as _time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any, Protocol, cast
from uuid import UUID

from core.domain.events.events import (
    DomainEvent,
    WorkerCostItem,
    WorkerResponseLatencyItem,
    WorkerTokenUsageItem,
    WorkerUsageMetrics,
)
from infrastructure.cleanup.registry import _pid_alive

from .base import WorkerAdapterBase
from .shared import (
    ContainerSessionContext,
    EventSequencer,
    UsageBreakdown,
    emit_cost,
    to_openhands_mcp_config,
)


# Suppress verbose OpenHands logging
logging.getLogger("openhands").setLevel(logging.WARNING)
logging.getLogger("openhands.sdk").setLevel(logging.WARNING)
logging.getLogger("openhands.tools").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS_PER_RUN = 20
_SHUTDOWN_GRACE_SECONDS = 5


def _event_dedup_key(sdk_event: Any) -> Any:
    """Stable dedup key for an SDK event.

    Prefer the SDK's own ``event.id`` when present; fall back to object identity.
    Identity is safe for the current SDK (callbacks receive the same object that
    gets appended to ``state.events``), but ``event.id`` survives any future wrap
    or copy by the SDK before append.
    """
    event_id = getattr(sdk_event, "id", None)
    if event_id is not None:
        return ("id", event_id)
    return ("obj", id(sdk_event))


_OPENHANDS_DEFAULT_CONTROL_TOOLS = ["FinishTool", "ThinkTool"]
_OPENHANDS_CONTAINER_AWARE_TOOL_NAMES = {"file_editor", "glob", "grep"}
_CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED = False
_CONTAINER_AWARE_OPENHANDS_TOOLS_LOCK = threading.Lock()
_TRAILING_PORT_DOT_PATTERN = re.compile(r":(?P<port>\d+)\.(?=$|/)")


def _normalize_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    stripped = base_url.strip()
    if not stripped:
        return None
    return _TRAILING_PORT_DOT_PATTERN.sub(r":\g<port>", stripped)


def _container_session_from_tool_params(
    container_session: object,
) -> ContainerSessionContext | None:
    if not isinstance(container_session, dict):
        return None
    try:
        return ContainerSessionContext.from_task_context({"container_session": container_session})
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "Ignoring invalid OpenHands container_session tool params",
            exc_info=True,
        )
        return None


def _map_openhands_action_paths(
    action: Any,
    container_session: ContainerSessionContext,
) -> Any:
    updates: dict[str, str] = {}
    path = getattr(action, "path", None)
    if isinstance(path, str):
        mapped_path = container_session.map_container_path(path)
        if mapped_path != path:
            updates["path"] = mapped_path

    if action.__class__.__name__ == "GlobAction":
        pattern = getattr(action, "pattern", None)
        if isinstance(pattern, str):
            mapped_pattern = container_session.map_container_path(pattern)
            if mapped_pattern != pattern:
                updates["pattern"] = mapped_pattern

    if not updates:
        return action

    model_copy = getattr(action, "model_copy", None)
    if callable(model_copy):
        return model_copy(update=updates)
    for key, value in updates.items():
        setattr(action, key, value)
    return action


def _replace_host_paths(
    text: str,
    container_session: ContainerSessionContext,
) -> str:
    return container_session.path_mapper.replace_host_paths(text)


def _sanitize_openhands_observation_paths(
    value: Any,
    container_session: ContainerSessionContext,
) -> Any:
    return container_session.path_mapper.sanitize_host_paths(value)


def _maybe_idempotent_file_create_observation(
    action: Any,
    container_session: ContainerSessionContext,
) -> Any | None:
    if getattr(action, "command", None) != "create":
        return None
    path = getattr(action, "path", None)
    file_text = getattr(action, "file_text", None)
    if not isinstance(path, str) or not isinstance(file_text, str):
        return None

    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        current = file_path.read_text()
    except UnicodeDecodeError:
        return None
    if current != file_text:
        return None

    from openhands.sdk.llm.message import TextContent
    from openhands.tools.file_editor.definition import FileEditorObservation

    container_path = _replace_host_paths(path, container_session)
    return FileEditorObservation(
        command="create",
        path=container_path,
        prev_exist=True,
        old_content=current,
        new_content=current,
        content=[
            TextContent(text=f"File already exists with identical content at: {container_path}")
        ],
    )


def _augment_native_tool_description(
    description: str,
    container_session: ContainerSessionContext,
) -> str:
    return (
        f"{description}\n\n"
        "Container path compatibility: use canonical container paths "
        f"`{container_session.container_source_dir}/...`, "
        f"`{container_session.container_testcase_dir}/...`, and "
        f"`{container_session.container_work_dir}/...`. This runtime maps them "
        "to the run mirror before execution and rewrites results back to "
        "container paths."
    )


def _ensure_container_aware_openhands_tools_registered() -> None:
    global _CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED
    if _CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED:
        return
    with _CONTAINER_AWARE_OPENHANDS_TOOLS_LOCK:
        if _CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED:
            return
        _register_container_aware_openhands_tools()
        _CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED = True


def _register_container_aware_openhands_tools() -> None:
    from openhands.sdk.tool import register_tool
    from openhands.sdk.tool.tool import ToolExecutor
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.glob import GlobTool
    from openhands.tools.grep import GrepTool

    class _PathMappingOpenHandsExecutor(ToolExecutor[Any, Any]):
        def __init__(
            self,
            delegate: ToolExecutor[Any, Any],
            container_session: ContainerSessionContext,
        ) -> None:
            self._delegate = delegate
            self._container_session = container_session

        def __call__(self, action: Any, conversation: Any | None = None) -> Any:
            mapped_action = _map_openhands_action_paths(
                action,
                self._container_session,
            )
            idempotent_observation = _maybe_idempotent_file_create_observation(
                mapped_action,
                self._container_session,
            )
            if idempotent_observation is not None:
                return idempotent_observation
            observation = self._delegate(mapped_action, conversation)
            return _sanitize_openhands_observation_paths(
                observation,
                self._container_session,
            )

        def close(self) -> None:
            self._delegate.close()

    def _wrap_native_tools(
        tools: Any,
        container_session: ContainerSessionContext,
    ) -> list[Any]:
        wrapped_tools: list[Any] = []
        for tool in tools:
            if tool.executor is None:
                wrapped_tools.append(tool)
                continue
            # Only the executor is swapped (it maps paths at call time). The
            # tool.description is left byte-identical across container sessions
            # so the static tool schema stays prompt-cacheable; the container
            # paths reach the agent via the task-description prefix instead
            # (see _prepare_task_description -> apply_task_prefix).
            wrapped_tools.append(
                tool.model_copy(
                    update={
                        "executor": _PathMappingOpenHandsExecutor(
                            tool.executor,
                            container_session,
                        ),
                    }
                )
            )
        return wrapped_tools

    class _ContainerAwareFileEditorTool(FileEditorTool):
        name = FileEditorTool.name

        @classmethod
        def create(
            cls,
            conv_state: Any,
            container_session: object = None,
        ) -> list[Any]:
            parsed_session = _container_session_from_tool_params(container_session)
            tools = FileEditorTool.create(conv_state)
            if parsed_session is None:
                return list(tools)
            return _wrap_native_tools(tools, parsed_session)

    class _ContainerAwareGlobTool(GlobTool):
        name = GlobTool.name

        @classmethod
        def create(
            cls,
            conv_state: Any,
            container_session: object = None,
        ) -> list[Any]:
            parsed_session = _container_session_from_tool_params(container_session)
            tools = GlobTool.create(conv_state)
            if parsed_session is None:
                return list(tools)
            return _wrap_native_tools(tools, parsed_session)

    class _ContainerAwareGrepTool(GrepTool):
        name = GrepTool.name

        @classmethod
        def create(
            cls,
            conv_state: Any,
            container_session: object = None,
        ) -> list[Any]:
            parsed_session = _container_session_from_tool_params(container_session)
            tools = GrepTool.create(conv_state)
            if parsed_session is None:
                return list(tools)
            return _wrap_native_tools(tools, parsed_session)

    # OpenHands pre-registers these native names; replacing them here is intentional.
    registry_logger = logging.getLogger("openhands.sdk.tool.registry")
    previous_level = registry_logger.level
    registry_logger.setLevel(max(previous_level, logging.ERROR))
    try:
        register_tool(FileEditorTool.name, _ContainerAwareFileEditorTool)
        register_tool(GlobTool.name, _ContainerAwareGlobTool)
        register_tool(GrepTool.name, _ContainerAwareGrepTool)
    finally:
        registry_logger.setLevel(previous_level)


def _snapshot_child_pids() -> set[int]:
    """Return the set of direct child PIDs of this process.

    Used to identify MCP stdio subprocesses spawned while OpenHands initializes
    and runs a conversation. OpenHands lazy-loads MCP tools during
    ``send_message()`` / ``run()``, so the caller snapshots before building the
    conversation and diffs against the current children during cleanup.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv is fully owned here.
            ["pgrep", "-P", str(os.getpid())],  # noqa: S607 - intentionally PATH-based.
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode not in (0, 1):
        return set()
    pids: set[int] = set()
    for line in completed.stdout.splitlines():
        token = line.strip()
        if not token:
            continue
        try:
            pids.add(int(token))
        except ValueError:
            continue
    return pids


class _ConversationLike(Protocol):
    """OpenHands SDK conversation surface used by this adapter."""

    def send_message(self, message: str) -> None: ...

    def run(self) -> object: ...


SDK_EVENT_TYPE_MAP: dict[str, str] = {
    "ActionEvent": "tool_use",
    "AgentThinkAction": "thinking",
    "MessageEvent": "output",
    "ObservationEvent": "tool_result",
    "CmdRunObservation": "tool_result",
    "AgentErrorEvent": "output",
    "FileReadAction": "tool_use",
    "FileWriteAction": "tool_use",
    "FileEditAction": "tool_use",
}


@dataclass(slots=True)
class OpenHandsCostData:
    """Normalized OpenHands cost data used to build WorkerCostRecorded events."""

    cost_usd: float = 0.0
    tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    usage_metrics: list[WorkerUsageMetrics] = field(default_factory=list)


@dataclass(slots=True)
class _OpenHandsConversationRun:
    """Own the blocking OpenHands SDK call and its one-thread boundary."""

    conversation: _ConversationLike
    task_description: str
    child_pid_baseline: set[int]
    timeout_seconds: int
    max_iterations_per_run: int
    _executor: concurrent.futures.ThreadPoolExecutor | None = field(default=None, init=False)
    _future: concurrent.futures.Future | None = field(default=None, init=False)

    def start(self) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="openhands"
        )
        self._future = self._executor.submit(self._send_message_and_run)

    def _send_message_and_run(self) -> object:
        self.conversation.send_message(self.task_description)
        return self.conversation.run()

    async def wait_for_completion(self) -> None:
        await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(self._future_or_raise())),
            timeout=self.timeout_seconds,
        )

    async def wait_for_exit_after_shutdown(self) -> None:
        future = self._future_or_raise()
        if future.done():
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=_SHUTDOWN_GRACE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "OpenHands thread did not exit within %ds after shutdown; "
                "it will run until max_iteration_per_run (%d) is reached",
                _SHUTDOWN_GRACE_SECONDS,
                self.max_iterations_per_run,
            )
        except Exception:
            logger.debug("OpenHands thread exit wait failed", exc_info=True)

    def current_child_pids(self) -> list[int]:
        return sorted(_snapshot_child_pids() - self.child_pid_baseline)

    def shutdown_executor(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    def _future_or_raise(self) -> concurrent.futures.Future:  # type: ignore[type-arg]
        if self._future is None:
            raise RuntimeError("OpenHands conversation run was not started")
        return self._future


@dataclass(frozen=True, slots=True)
class _OpenHandsRunOutcome:
    """Result of waiting for the blocking OpenHands SDK call."""

    timed_out: bool
    shutdown_requested: bool = False


class OpenHandsAdapter(WorkerAdapterBase):
    """Execute tasks via the OpenHands SDK with native conversation controls."""

    STREAM_NAME = "openhands"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 300,
        max_iterations_per_run: int = DEFAULT_MAX_ITERATIONS_PER_RUN,
        base_url: str | None = None,
        allowed_tools: list[str] | None = None,
        mcp_tools: list[str] | None = None,
    ) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        if not model:
            raise ValueError("OpenHandsAdapter requires an explicit model")
        self.model: str = model
        self.api_key = api_key or self._detect_api_key()
        self.max_iterations_per_run: int = max_iterations_per_run
        self.base_url: str | None = _normalize_base_url(base_url)
        self.allowed_tools: list[str] = allowed_tools or []
        self.mcp_tools: list[str] = mcp_tools or []

    def _detect_api_key(self) -> str | None:
        if api_key := os.getenv("LLM_API_KEY"):
            return api_key

        model_lower = self.model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return os.getenv("ANTHROPIC_API_KEY")
        if "gemini" in model_lower or "google" in model_lower:
            return os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if model_lower.startswith(("ollama/", "ollama_chat/")) or "ollama" in model_lower:
            return os.getenv("OLLAMA_API_KEY")
        return os.getenv("OPENAI_API_KEY")

    def _get_tool_name(self) -> str:
        return "openhands"

    async def _execute_task(
        self,
        task_description: str,
        agent_id: UUID,
        working_dir: str,
        sequencer: EventSequencer,
        task_context: dict[str, Any],
    ) -> AsyncIterator[DomainEvent]:
        _ = agent_id  # Contract parameter; EventSequencer already carries this ID.
        started_at = time()
        container_session = ContainerSessionContext.from_task_context(task_context)
        task_description = self._prepare_task_description(
            task_description,
            container_session,
        )
        mcp_servers = self._extract_mcp_servers(task_context)
        children_before = _snapshot_child_pids()

        # SDK callbacks fire synchronously from the OpenHands worker thread.
        # Bridge them onto the running event loop so the async generator can
        # yield ThoughtCaptured events as they happen, instead of after the
        # whole conversation finishes (see openhands.sdk Conversation.callbacks).
        loop = asyncio.get_running_loop()
        event_queue: asyncio.Queue[Any] = asyncio.Queue()

        def sdk_event_callback(sdk_event: Any) -> None:
            try:
                loop.call_soon_threadsafe(event_queue.put_nowait, sdk_event)
            except RuntimeError:
                # Loop closed during shutdown; drop the event quietly.
                pass

        try:
            conversation = self._build_conversation_for_task(
                working_dir,
                mcp_servers,
                container_session,
                callbacks=[sdk_event_callback],
            )
        except ImportError as error:
            yield sequencer.failed(
                "OpenHands packages not installed. "
                f"Install with: uv add openhands-sdk openhands-tools. Error: {error}"
            )
            return
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {error!r}")
            return

        conversation_run = self._create_conversation_run(
            conversation=conversation,
            task_description=task_description,
            children_before=children_before,
        )
        shutdown_requested = False
        run_task: asyncio.Task[_OpenHandsRunOutcome] | None = None
        seen_event_ids: set[int] = set()
        try:
            run_task = asyncio.create_task(
                self._run_conversation_or_timeout(conversation, conversation_run)
            )

            # Stream events as the SDK fires callbacks; interleave with run completion.
            while not run_task.done():
                try:
                    sdk_event = await asyncio.wait_for(event_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                seen_event_ids.add(_event_dedup_key(sdk_event))
                if thought := self._sdk_event_to_thought(sdk_event, sequencer):
                    yield thought

            # Drain any callbacks that landed after the last loop check.
            while not event_queue.empty():
                sdk_event = event_queue.get_nowait()
                seen_event_ids.add(_event_dedup_key(sdk_event))
                if thought := self._sdk_event_to_thought(sdk_event, sequencer):
                    yield thought

            outcome = await run_task
            shutdown_requested = outcome.shutdown_requested
            if outcome.timed_out:
                yield sequencer.failed(f"Task timed out after {self.timeout_seconds} seconds")
                return

            # Fallback: yield any events that landed in ``state.events`` but never
            # came through the callback path (test fakes, SDK quirks, terminal
            # events emitted post-callback). Dedup by object identity.
            for sdk_event in self._iter_conversation_events(conversation):
                if _event_dedup_key(sdk_event) in seen_event_ids:
                    continue
                if thought := self._sdk_event_to_thought(sdk_event, sequencer):
                    yield thought

            finish_message = self._extract_finish_message(conversation)
            yield self._make_cost_recorded_event(conversation, sequencer, started_at)
            yield self._make_completed_event(
                conversation,
                working_dir,
                finish_message,
                sequencer,
            )
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {error!r}")
        finally:
            if run_task is not None and not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except (asyncio.CancelledError, Exception):
                    pass
            if not shutdown_requested:
                await self._shutdown_conversation_run(conversation, conversation_run)
            conversation_run.shutdown_executor()

    @staticmethod
    def _prepare_task_description(
        task_description: str,
        container_session: ContainerSessionContext | None,
    ) -> str:
        if container_session is None:
            return task_description
        return container_session.apply_task_prefix(task_description, auto_shell=False)

    def _build_conversation_for_task(
        self,
        working_dir: str,
        mcp_servers: dict[str, dict[str, Any]] | None,
        container_session: ContainerSessionContext | None,
        callbacks: list[Callable[[Any], None]] | None = None,
    ) -> _ConversationLike:
        # Pass ``callbacks`` positionally so existing test fakes that monkey-patch
        # ``_build_conversation`` with ``lambda *_args: conversation`` continue to
        # work without **kwargs support.
        return cast(
            "_ConversationLike",
            self._build_conversation(working_dir, mcp_servers, container_session, callbacks),
        )

    def _create_conversation_run(
        self,
        *,
        conversation: _ConversationLike,
        task_description: str,
        children_before: set[int],
    ) -> _OpenHandsConversationRun:
        return _OpenHandsConversationRun(
            conversation=conversation,
            task_description=task_description,
            child_pid_baseline=children_before,
            timeout_seconds=self.timeout_seconds,
            max_iterations_per_run=self.max_iterations_per_run,
        )

    async def _run_conversation_or_timeout(
        self,
        conversation: _ConversationLike,
        conversation_run: _OpenHandsConversationRun,
    ) -> _OpenHandsRunOutcome:
        conversation_run.start()
        try:
            await conversation_run.wait_for_completion()
        except TimeoutError:
            await self._shutdown_conversation_run(conversation, conversation_run)
            await conversation_run.wait_for_exit_after_shutdown()
            return _OpenHandsRunOutcome(timed_out=True, shutdown_requested=True)
        return _OpenHandsRunOutcome(timed_out=False)

    async def _shutdown_conversation_run(
        self,
        conversation: _ConversationLike,
        conversation_run: _OpenHandsConversationRun,
    ) -> None:
        await self._request_shutdown_async(
            conversation,
            mcp_child_pids=conversation_run.current_child_pids(),
        )

    def _iter_conversation_domain_events(
        self,
        conversation: _ConversationLike,
        sequencer: EventSequencer,
    ) -> Iterator[DomainEvent]:
        for sdk_event in self._iter_conversation_events(conversation):
            if thought_event := self._sdk_event_to_thought(sdk_event, sequencer):
                yield thought_event

    def _extract_finish_message(self, conversation: _ConversationLike) -> str | None:
        for sdk_event in self._iter_conversation_events(conversation):
            finish_message = self._try_extract_finish(sdk_event)
            if finish_message is not None:
                return finish_message
        return None

    def _sdk_event_to_thought(
        self,
        sdk_event: Any,
        sequencer: EventSequencer,
    ) -> DomainEvent | None:
        content = self._extract_event_content(sdk_event)
        if not content or not content.strip():
            return None

        output_type = self._classify_event(sdk_event)
        tool_name = self._extract_tool_name(sdk_event) if output_type == "tool_use" else None
        return sequencer.thought(content.strip(), output_type, tool_name=tool_name)

    def _make_cost_recorded_event(
        self,
        conversation: _ConversationLike,
        sequencer: EventSequencer,
        started_at: float,
    ) -> DomainEvent:
        cost_data = self._extract_cost_data(conversation)
        return emit_cost(
            sequencer,
            tool_name=self._get_tool_name(),
            duration_seconds=time() - started_at,
            model=self._resolve_cost_model(cost_data),
            breakdown=UsageBreakdown(
                prompt_tokens=cost_data.prompt_tokens,
                completion_tokens=cost_data.completion_tokens,
                cache_read_tokens=cost_data.cache_read_tokens,
                cache_write_tokens=cost_data.cache_write_tokens,
                reasoning_tokens=cost_data.reasoning_tokens,
                cost_usd=cost_data.cost_usd,
            ),
            usage_metrics=cost_data.usage_metrics,
        )

    def _make_completed_event(
        self,
        conversation: _ConversationLike,
        working_dir: str,
        finish_message: str | None,
        sequencer: EventSequencer,
    ) -> DomainEvent:
        return sequencer.completed(self._extract_result(conversation, working_dir, finish_message))

    def _build_conversation(
        self,
        working_dir: str,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        container_session: ContainerSessionContext | None = None,
        callbacks: list[Callable[[Any], None]] | None = None,
    ) -> Any:
        import warnings

        # Suppress authlib.jose deprecation from openhands.sdk.llm.auth.openai
        # — upstream issue, cannot fix on our side.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="authlib.jose module is deprecated",
                category=DeprecationWarning,
            )
            from openhands.sdk import LLM, Agent, Conversation, Tool

        llm = LLM(**self._build_llm_kwargs())
        tools = self._build_native_tools(Tool, container_session)
        # Agent creates MCP tool definitions from this config after native tools.
        mcp_config = to_openhands_mcp_config(mcp_servers) if mcp_servers else {}

        agent = Agent(
            llm=llm,
            tools=tools,
            mcp_config=mcp_config,
            include_default_tools=_OPENHANDS_DEFAULT_CONTROL_TOOLS,
        )
        return Conversation(
            agent=agent,
            workspace=working_dir,
            max_iteration_per_run=self.max_iterations_per_run,
            callbacks=callbacks or [],
        )

    def _build_llm_kwargs(self) -> dict[str, Any]:
        # caching_prompt enables Anthropic prompt caching in the OpenHands SDK.
        # It is the SDK default, but set explicitly here so the intent is
        # visible; the SDK gates it by model support, so it is a safe no-op for
        # non-Anthropic models.
        llm_kwargs: dict[str, Any] = {
            "model": self.model,
            "api_key": self.api_key,
            "caching_prompt": True,
        }
        if base_url := self._effective_base_url():
            llm_kwargs["base_url"] = base_url
        if self._is_ollama_model():
            llm_kwargs.update(self._ollama_llm_overrides())
        return llm_kwargs

    def _effective_base_url(self) -> str | None:
        if self.base_url:
            return self.base_url
        if self._is_ollama_model():
            return _normalize_base_url(os.getenv("OLLAMA_API_BASE"))
        return None

    def _is_ollama_model(self) -> bool:
        return self.model.lower().startswith(("ollama/", "ollama_chat/"))

    @staticmethod
    def _ollama_llm_overrides() -> dict[str, Any]:
        """Keep Ollama/Qwen compatible with OpenHands' multi-turn tool loop.

        ``num_ctx`` lowered from 65536 → 16384: on Apple Silicon the KV cache
        for 65K context against an 8B model makes per-iteration latency ~3-4x
        worse than the smaller window. OpenHands condensation handles overflow,
        so the smaller window is safe for the file_editor/glob/grep workflow.

        ``max_output_tokens`` lowered from 65000 → 8192: nonsensical when
        num_ctx is 16384 (Ollama clamps to ctx-minus-prompt anyway). 8192 is
        plenty for one OpenHands turn (a thought + tool-call or a final answer)
        and signals intent clearly to LiteLLM.
        """
        return {
            "max_output_tokens": 8192,
            "native_tool_calling": False,
            # This disables model-side reasoning tags; OpenHands ThinkTool remains enabled.
            "litellm_extra_body": {"think": False, "num_ctx": 16384},
            "top_p": 0.95,
        }

    def _build_native_tools(
        self,
        tool_factory: Callable[..., Any],
        container_session: ContainerSessionContext | None = None,
    ) -> list[Any]:
        native_tool_names = self._openhands_native_tool_names()
        unknown = [name for name in self.allowed_tools if name not in native_tool_names]
        if unknown:
            allowed = ", ".join(sorted(native_tool_names))
            requested = ", ".join(unknown)
            raise ValueError(
                f"Unsupported OpenHands native tool(s): {requested}. "
                f"Configured worker.allowed_tools must use: {allowed}."
            )
        container_session_params: dict[str, str] | None = None
        if container_session is not None and any(
            name in _OPENHANDS_CONTAINER_AWARE_TOOL_NAMES for name in self.allowed_tools
        ):
            _ensure_container_aware_openhands_tools_registered()
            container_session_params = self._container_session_tool_params(
                container_session,
            )

        tools: list[Any] = []
        for name in self.allowed_tools:
            if (
                container_session_params is not None
                and name in _OPENHANDS_CONTAINER_AWARE_TOOL_NAMES
            ):
                tools.append(
                    tool_factory(
                        name=name,
                        params={"container_session": container_session_params},
                    )
                )
                continue
            tools.append(tool_factory(name=name))
        return tools

    @staticmethod
    def _container_session_tool_params(
        container_session: ContainerSessionContext,
    ) -> dict[str, str]:
        return {
            "container_id": container_session.container_id,
            "container_name": container_session.container_name,
            "image": container_session.image,
            "workspace_root": str(container_session.workspace_root),
            "host_source_dir": str(container_session.host_source_dir),
            "host_testcase_dir": str(container_session.host_testcase_dir),
            "host_work_root": str(container_session.host_work_root),
            "host_work_dir": str(container_session.host_work_dir),
            "container_source_dir": container_session.container_source_dir,
            "container_testcase_dir": container_session.container_testcase_dir,
            "container_work_dir": container_session.container_work_dir,
            "container_working_directory": (container_session.container_working_directory),
            "helper_script": str(container_session.helper_script),
            "container_workspace_root": container_session.container_workspace_root,
        }

    @staticmethod
    def _openhands_native_tool_names() -> set[str]:
        # Keep host-shell access out of OpenHands native tools. Container shell
        # commands must use the domain plugin's MCP ``shell_in_container`` tool.
        from openhands.tools.file_editor import FileEditorTool
        from openhands.tools.glob import GlobTool
        from openhands.tools.grep import GrepTool

        return {FileEditorTool.name, GlobTool.name, GrepTool.name}

    @staticmethod
    def _extract_mcp_servers(
        task_context: dict[str, Any],
    ) -> dict[str, dict[str, Any]] | None:
        servers = task_context.get("mcp_servers")
        if not isinstance(servers, dict) or not servers:
            return None
        return dict(servers)

    def _iter_conversation_events(self, conversation: Any) -> list[Any]:
        state = getattr(conversation, "state", None)
        if state is None or not hasattr(state, "events"):
            return []
        return list(state.events)

    def _extract_cost_data(self, conversation: Any) -> OpenHandsCostData:
        stats = getattr(conversation, "conversation_stats", None)
        if stats is None:
            return OpenHandsCostData()

        usage_to_metrics = self._get_stat(stats, "usage_to_metrics")
        return (
            self._extract_usage_metrics(usage_to_metrics)
            if usage_to_metrics
            else OpenHandsCostData()
        )

    def _extract_usage_metrics(self, usage_to_metrics: Any) -> OpenHandsCostData:
        items = usage_to_metrics.items() if isinstance(usage_to_metrics, dict) else []
        usage_metrics: list[WorkerUsageMetrics] = []
        total_cost = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0

        for usage_id, metrics in items:
            usage_metric = self._build_usage_metrics(str(usage_id), metrics)
            usage_metrics.append(usage_metric)
            total_cost += usage_metric.accumulated_cost_usd
            prompt_tokens += usage_metric.prompt_tokens
            completion_tokens += usage_metric.completion_tokens
            cache_read_tokens += usage_metric.cache_read_tokens
            cache_write_tokens += usage_metric.cache_write_tokens
            reasoning_tokens += usage_metric.reasoning_tokens

        if not usage_metrics:
            return OpenHandsCostData()

        return OpenHandsCostData(
            cost_usd=total_cost,
            tokens=UsageBreakdown(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
            ).tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_metrics=usage_metrics,
        )

    def _build_usage_metrics(self, usage_id: str, metrics: Any) -> WorkerUsageMetrics:
        """Normalize one Metrics or MetricsSnapshot record into event payload data."""
        model = self._get_stat(metrics, "model_name")
        accumulated_cost = float(self._get_stat(metrics, "accumulated_cost", 0.0) or 0.0)
        (
            prompt_tokens,
            completion_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
        ) = self._extract_accumulated_token_usage(metrics)

        return WorkerUsageMetrics(
            usage_id=usage_id,
            model=model,
            accumulated_cost_usd=accumulated_cost,
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            cache_read_tokens=cache_read_tokens or 0,
            cache_write_tokens=cache_write_tokens or 0,
            reasoning_tokens=reasoning_tokens or 0,
            cost_items=[
                WorkerCostItem(
                    model=self._get_stat(cost, "model", "") or model or "",
                    cost_usd=float(
                        self._get_stat(cost, "cost_usd", self._get_stat(cost, "cost", 0.0)) or 0.0
                    ),
                    timestamp=self._maybe_float(self._get_stat(cost, "timestamp")),
                )
                for cost in self._get_stat(metrics, "costs", []) or []
            ],
            response_latencies=[
                WorkerResponseLatencyItem(
                    model=self._get_stat(latency, "model", "") or model or "",
                    latency_seconds=float(self._get_stat(latency, "latency", 0.0) or 0.0),
                    response_id=str(self._get_stat(latency, "response_id", "") or ""),
                )
                for latency in self._get_stat(metrics, "response_latencies", []) or []
            ],
            token_usages=[
                WorkerTokenUsageItem(
                    model=self._get_stat(token_usage, "model", "") or model or "",
                    prompt_tokens=int(self._get_stat(token_usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(self._get_stat(token_usage, "completion_tokens", 0) or 0),
                    cache_read_tokens=int(self._get_stat(token_usage, "cache_read_tokens", 0) or 0),
                    cache_write_tokens=int(
                        self._get_stat(token_usage, "cache_write_tokens", 0) or 0
                    ),
                    reasoning_tokens=int(self._get_stat(token_usage, "reasoning_tokens", 0) or 0),
                    context_window=int(self._get_stat(token_usage, "context_window", 0) or 0),
                    per_turn_token=int(self._get_stat(token_usage, "per_turn_token", 0) or 0),
                    response_id=str(self._get_stat(token_usage, "response_id", "") or ""),
                )
                for token_usage in self._get_stat(metrics, "token_usages", []) or []
            ],
        )

    def _extract_accumulated_token_usage(
        self,
        stats: Any,
    ) -> tuple[int | None, int | None, int | None, int | None, int | None]:
        accumulated = self._get_stat(stats, "accumulated_token_usage")
        if accumulated is not None:
            return (
                self._maybe_int(self._get_stat(accumulated, "prompt_tokens")),
                self._maybe_int(self._get_stat(accumulated, "completion_tokens")),
                self._maybe_int(self._get_stat(accumulated, "cache_read_tokens")),
                self._maybe_int(self._get_stat(accumulated, "cache_write_tokens")),
                self._maybe_int(self._get_stat(accumulated, "reasoning_tokens")),
            )

        return (
            self._maybe_int(self._get_stat(stats, "prompt_tokens")),
            self._maybe_int(self._get_stat(stats, "completion_tokens")),
            self._maybe_int(self._get_stat(stats, "cache_read_tokens")),
            self._maybe_int(self._get_stat(stats, "cache_write_tokens")),
            self._maybe_int(self._get_stat(stats, "reasoning_tokens")),
        )

    def _resolve_cost_model(self, cost_data: OpenHandsCostData) -> str | None:
        usage_models = {usage.model for usage in cost_data.usage_metrics if usage.model}
        if len(usage_models) == 1:
            return next(iter(usage_models))
        return self.model

    @staticmethod
    def _get_stat(obj: Any, name: str, default: Any = None) -> Any:
        """Read an attribute or dict key without assuming an SDK object shape."""
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @staticmethod
    def _maybe_int(value: Any) -> int | None:
        """Normalize optional numeric values to integers."""
        if value is None:
            return None
        return int(value)

    @staticmethod
    def _maybe_float(value: Any) -> float | None:
        """Normalize optional numeric values to floats."""
        if value is None:
            return None
        return float(value)

    _EVENT_TEXT_LIMIT = 600

    def _extract_result(
        self,
        conversation: Any,
        working_dir: str,
        finish_message: str | None = None,
    ) -> str:
        """Build a compact command log from ALL conversation events.

        Instead of dumping raw SDK repr for a few events, this creates a
        structured ``$ command (exit N)`` + truncated output summary for
        every output event. The verification judge can then see the full
        execution timeline at a glance.
        """
        entries: list[str] = []
        for event in self._iter_conversation_events(conversation):
            if self._classify_event(event) not in {"output", "tool_result"}:
                continue
            summary = self._compact_observation(event)
            if summary:
                entries.append(summary)

        parts: list[str] = []
        if entries:
            parts.extend(entries)
        if finish_message:
            parts.append(f"--- Agent Conclusion ---\n{finish_message}")
        if parts:
            return "\n".join(parts)
        return f"Task completed in workspace: {working_dir}"

    @classmethod
    def _compact_observation(cls, event: Any) -> str | None:
        """Extract command, exit code, and truncated text from an SDK event."""
        obs = getattr(event, "observation", None)
        if obs is None:
            raw = cls._extract_event_content(event)
            if not raw:
                return None
            return cls._truncate_text(raw, cls._EVENT_TEXT_LIMIT)

        cmd = getattr(obs, "command", "") or ""
        metadata = getattr(obs, "metadata", None)
        exit_code = getattr(metadata, "exit_code", None) if metadata else None
        text = cls._observation_text(obs)

        header = f"$ {cmd} (exit {exit_code})" if cmd else ""
        if text:
            text = cls._truncate_text(text, cls._EVENT_TEXT_LIMIT)

        if header and text:
            return f"{header}\n{text}"
        return header or text or None

    @staticmethod
    def _observation_text(obs: Any) -> str:
        content = getattr(obs, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    return str(text)
        return str(content)

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        """Head+tail truncation of a single text block."""
        if len(text) <= limit:
            return text
        half = limit // 2
        return f"{text[:half]}\n[...{len(text) - limit} chars truncated...]\n{text[-half:]}"

    @staticmethod
    def _try_extract_finish(event: Any) -> str | None:
        action = getattr(event, "action", None)
        if action is None:
            return None
        if type(action).__name__ != "FinishAction":
            return None
        msg = getattr(action, "message", None)
        return str(msg).strip() if msg and str(msg).strip() else None

    async def _request_shutdown_async(
        self,
        conversation: Any,
        *,
        mcp_child_pids: list[int] | None = None,
    ) -> None:
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="openhands-shutdown"
        )
        future = executor.submit(
            self._request_shutdown,
            conversation,
            mcp_child_pids=mcp_child_pids,
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(future)),
                timeout=(float(_SHUTDOWN_GRACE_SECONDS) * 2) + 1.0,
            )
        except TimeoutError:
            logger.warning(
                "OpenHands shutdown did not finish within %.0fs; cleanup will "
                "continue in its background thread",
                (float(_SHUTDOWN_GRACE_SECONDS) * 2) + 1.0,
            )
        except Exception:
            logger.debug("OpenHands shutdown failed", exc_info=True)
        finally:
            executor.shutdown(wait=False)

    def _request_shutdown(
        self,
        conversation: Any,
        *,
        mcp_child_pids: list[int] | None = None,
    ) -> None:
        """Best-effort SDK-native cleanup for the conversation.

        After the SDK-native ``pause()`` / ``close()`` path runs, SIGTERM any
        MCP stdio subprocesses that survived. ``Conversation.close()`` is
        best-effort with respect to MCP children, so the reaper is wired
        unconditionally. ``_pid_alive`` short-circuits the kill when the SDK
        already terminated the child, so the reaper is a true no-op in the
        happy path.
        """
        for method_name in ("pause", "close"):
            method = getattr(conversation, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    logger.debug(
                        "OpenHands conversation %s() failed during cleanup",
                        method_name,
                        exc_info=True,
                    )

        if not mcp_child_pids:
            return
        self._reap_with_escalation(set(mcp_child_pids))

    def _reap_with_escalation(  # noqa: PLR0912 - explicit OS cleanup branches.
        self,
        pids: set[int],
        *,
        grace_seconds: float = float(_SHUTDOWN_GRACE_SECONDS),
        poll_interval: float = 0.1,
    ) -> None:
        """SIGTERM ``pids``, poll ``waitpid``, then SIGKILL survivors.

        Codex review #2: the previous reaper sent SIGTERM and returned
        without reaping. SDK-spawned MCP children that exited stayed as
        zombies, and children that ignored SIGTERM stayed live. Both
        accumulate across long matrix runs.

        Steps:
          1. SIGTERM every still-alive PID (uses ``_pid_alive`` to skip
             ones the SDK already cleaned up).
          2. Poll ``os.waitpid(pid, WNOHANG)`` every ``poll_interval``
             seconds until either every PID is reaped or
             ``grace_seconds`` elapse. ``waitpid`` returns ``(0, 0)``
             when the child is still running; ``(pid, _)`` once reaped;
             raises ``ChildProcessError`` if the OS already reaped it.
          3. For any PID still alive after the grace, SIGKILL and drain
             ``waitpid`` again (best-effort).
          4. Treat ``ChildProcessError`` and ``ProcessLookupError`` as
             "already gone." All other ``OSError`` is logged at debug.
        """
        if not pids:
            return

        # Step 1: SIGTERM.
        for pid in list(pids):
            if not _pid_alive(pid):
                pids.discard(pid)
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pids.discard(pid)
            except OSError:
                logger.debug("SIGTERM to MCP child PID %d failed", pid, exc_info=True)

        # Step 2: poll waitpid until grace expires.
        deadline = _time.monotonic() + grace_seconds
        while pids and _time.monotonic() < deadline:
            self._drain_waitpid(pids)
            if pids:
                _time.sleep(poll_interval)

        if not pids:
            return

        # Step 3: SIGKILL survivors.
        for pid in list(pids):
            if not _pid_alive(pid):
                pids.discard(pid)
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pids.discard(pid)
            except OSError:
                logger.debug("SIGKILL to MCP child PID %d failed", pid, exc_info=True)

        # Step 4: drain waitpid one more time so SIGKILLed children don't
        # linger as zombies waiting for the parent to reap them.
        kill_deadline = _time.monotonic() + grace_seconds
        while pids and _time.monotonic() < kill_deadline:
            self._drain_waitpid(pids)
            if pids:
                _time.sleep(poll_interval)

    @staticmethod
    def _drain_waitpid(pids: set[int]) -> None:
        """Non-blocking ``waitpid`` for each PID; discard those reaped."""
        for pid in list(pids):
            try:
                wpid, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                # The OS already reaped this child (e.g., handled by
                # another waiter or by ``signal.SIGCHLD`` default).
                pids.discard(pid)
                continue
            except OSError:
                logger.debug("waitpid for MCP child PID %d failed", pid, exc_info=True)
                continue
            if wpid == pid:
                pids.discard(pid)

    @staticmethod
    def _classify_event(event: Any) -> str:
        if getattr(event, "action", None) is not None:
            return "tool_use"
        if getattr(event, "observation", None) is not None:
            return "tool_result"
        event_class_name = type(event).__name__
        return SDK_EVENT_TYPE_MAP.get(event_class_name, "output")

    @classmethod
    def _extract_event_content(cls, event: Any) -> str | None:
        if hasattr(event, "action") and event.action:
            return cls._format_action_event(event.action)
        if hasattr(event, "observation") and event.observation:
            return cls._format_observation_event(event.observation)
        if hasattr(event, "message") and event.message:
            return str(event.message)
        if hasattr(event, "content") and event.content:
            return str(event.content)
        if hasattr(event, "thought") and event.thought:
            return f"Thought: {event.thought}"
        return None

    @staticmethod
    def _extract_tool_name(event: Any) -> str | None:
        action = getattr(event, "action", None)
        if action is None:
            return None
        return type(action).__name__ or "OpenHandsAction"

    @classmethod
    def _format_action_event(cls, action: Any) -> str:
        tool_name = type(action).__name__ or "OpenHandsAction"
        payload: dict[str, Any] = {}
        for attr_name in ("command", "path", "file_path", "thought", "content"):
            value = getattr(action, attr_name, None)
            if value:
                payload[attr_name] = str(value)
        detail = (
            payload if payload else {"repr": cls._truncate_text(str(action), cls._EVENT_TEXT_LIMIT)}
        )
        return f"Tool: {tool_name}\nInput: {json_dumps_for_event(detail)}"

    @classmethod
    def _format_observation_event(cls, observation: Any) -> str:
        command = getattr(observation, "command", "") or ""
        metadata = getattr(observation, "metadata", None)
        exit_code = getattr(metadata, "exit_code", None) if metadata else None
        text = cls._truncate_text(cls._observation_text(observation), cls._EVENT_TEXT_LIMIT)
        parts = []
        if command:
            parts.append(f"command={command}")
        if exit_code is not None:
            parts.append(f"exit_code={exit_code}")
        header = "; ".join(parts) or type(observation).__name__
        return f"Tool result: {header}\n{text}".rstrip()


def json_dumps_for_event(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, default=str)
