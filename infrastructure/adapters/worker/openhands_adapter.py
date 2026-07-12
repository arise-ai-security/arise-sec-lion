"""OpenHands SDK adapter: use the SDK conversation directly for task execution."""

import asyncio
import concurrent.futures
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from time import time
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from core.domain.events.events import DomainEvent
from infrastructure.adapters.worker.shared_code_context import SharedCodeContextProvider

from .base import WorkerAdapterBase
from .openhands_container_tools import _ensure_container_aware_openhands_tools_registered
from .openhands_cost import make_cost_recorded_event
from .openhands_events import (
    classify_event,
    extract_event_content,
    extract_finish_message,
    extract_result,
    iter_conversation_events,
    make_completed_event,
    observation_text,
    sdk_event_to_thought,
)
from .process_reaper import (
    SHUTDOWN_GRACE_SECONDS,
    reap_with_escalation,
    snapshot_child_pids,
)
from .shared import (
    ContainerSessionContext,
    EventSequencer,
    to_openhands_mcp_config,
)
from .shared.errors import describe_error


# Suppress verbose OpenHands logging
logging.getLogger("openhands").setLevel(logging.WARNING)
logging.getLogger("openhands.sdk").setLevel(logging.WARNING)
logging.getLogger("openhands.tools").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS_PER_RUN = 20


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
# FileEditorObservation.command values that surface source content to capture
# (full-file ``view``) vs. mutate a file (its recorded content is invalidated).
_FILE_VIEW_COMMANDS = frozenset({"view"})
_FILE_EDIT_COMMANDS = frozenset({"str_replace", "create", "insert", "undo_edit"})

# First line of the OpenHands file-editor observation when ``view`` targets a
# directory. Used to keep directory snapshots out of the shared code block —
# they go stale on the next file write and duplicate the workspace listing.
_DIRECTORY_LISTING_VIEW_PREFIX = "Here's the files and directories up to"
_TRAILING_PORT_DOT_PATTERN = re.compile(r":(?P<port>\d+)\.(?=$|/)")

# OpenHands renders file-view content ``cat -n`` framed: each line is a right-
# justified 1-indexed number, a tab, then the text (editor._make_output). The
# first/last numbers give the actual line span the observation carried.
_CAT_N_LINE_NUMBER_PATTERN = re.compile(r"^\s*(\d+)\t", re.MULTILINE)
# Stable marker OpenHands inserts when a view is clipped to the output limit
# (openhands.tools.file_editor.utils.constants.TEXT_FILE_CONTENT_TRUNCATED_NOTICE).
_VIEW_TRUNCATED_MARKER = "<response clipped>"


def _classify_view_capture(
    content: str, requested: list[int] | None
) -> tuple[
    Literal["full", "range", "truncated"], int | None, int | None, int | None, int | None
]:
    """Classify an OpenHands file ``view`` result into a truthful capture type.

    Returns ``(capture_type, requested_start, requested_end, actual_start,
    actual_end)``. The actual span comes from the ``cat -n`` line numbers in the
    content; the requested span from the view action's ``view_range``. A view
    with no requested slice, starting at line 1 and not clipped, is a verified
    full-file read; a requested slice, a mid-file start, or a clip marker makes
    it an excerpt (``range``/``truncated``) so the block never presents it as a
    complete file.
    """
    numbers = [int(n) for n in _CAT_N_LINE_NUMBER_PATTERN.findall(content)]
    actual_start = numbers[0] if numbers else None
    actual_end = numbers[-1] if numbers else None
    clipped = _VIEW_TRUNCATED_MARKER in content

    req_start: int | None = None
    req_end: int | None = None
    if requested and len(requested) == 2:
        req_start, req_end = requested[0], requested[1]

    # A view_range of [1, -1] (or an omitted bound) requests the whole file.
    whole_file_request = (req_start is None or req_start <= 1) and (
        req_end is None or req_end == -1
    )
    partial = clipped or not whole_file_request or (actual_start is not None and actual_start > 1)
    if not partial:
        capture_type = "full"
    elif clipped and whole_file_request:
        capture_type = "truncated"
    else:
        capture_type = "range"
    return capture_type, req_start, req_end, actual_start, actual_end


def _normalize_base_url(base_url: str | None) -> str | None:
    if base_url is None:
        return None
    stripped = base_url.strip()
    if not stripped:
        return None
    return _TRAILING_PORT_DOT_PATTERN.sub(r":\g<port>", stripped)


class _ConversationLike(Protocol):
    """OpenHands SDK conversation surface used by this adapter."""

    def send_message(self, message: str) -> None: ...

    def run(self) -> object: ...


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
                timeout=SHUTDOWN_GRACE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "OpenHands thread did not exit within %ds after shutdown; "
                "it will run until max_iteration_per_run (%d) is reached",
                SHUTDOWN_GRACE_SECONDS,
                self.max_iterations_per_run,
            )
        except Exception:
            logger.debug("OpenHands thread exit wait failed", exc_info=True)

    def current_child_pids(self) -> list[int]:
        return sorted(snapshot_child_pids() - self.child_pid_baseline)

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


@dataclass(slots=True)
class _SharedCodeCapture:
    """Per-task binding for capturing file views/edits into the shared code context.

    ``port`` is the shared-code provider bound to ``queue`` as its emit sink, so
    each ``record_view`` / ``record_edit`` enqueues the built domain event for the
    adapter to *yield* into the worker stream (re-sequenced on the live aggregate,
    never an out-of-band append — Invariant D).
    """

    port: SharedCodeContextProvider
    root_id: UUID
    agent_id: UUID
    queue: asyncio.Queue[DomainEvent]
    # Last ``view`` action's requested ``view_range`` per path, awaiting its
    # observation. Absence => the model requested no range (a verified full-file
    # view); presence => a slice, so the capture is recorded as an excerpt.
    pending_view_ranges: dict[str, list[int] | None] = field(default_factory=dict)


def _conversation_identity(conversation: _ConversationLike) -> str:
    """Stable identity for a built Conversation, for DB-level invariant checks.

    Prefers an SDK-provided id; falls back to a fresh UUID minted once per built
    Conversation object (distinct workers building distinct conversations get
    distinct ids by construction).
    """
    for candidate in (
        getattr(conversation, "id", None),
        getattr(getattr(conversation, "state", None), "id", None),
    ):
        if candidate:
            return str(candidate)
    return str(uuid4())


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
        mcp_tool_timeout_seconds: int = 600,
        enable_subagents: bool = False,
        shared_code_port: SharedCodeContextProvider | None = None,
        run_scoped_cache_key: bool = False,
        reasoning_effort: str | None = None,
        reasoning_effort_overrides: dict[str, str] | None = None,
        skip_directory_view_capture: bool = False,
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
        # When True, every conversation in a run pre-pins the SDK's OpenAI
        # prompt_cache_key to the run root id, so all workers share one
        # prefix-cache shard. The SDK otherwise pins per-conversation, which
        # defeats cross-worker prefix-cache reuse (each worker = own shard).
        self._run_scoped_cache_key: bool = run_scoped_cache_key
        # None keeps the SDK default ("high"). Overrides map a task-description
        # prefix (e.g. "[Exploiter]") to an effort; first match wins.
        self._reasoning_effort: str | None = reasoning_effort
        self._reasoning_effort_overrides: dict[str, str] = dict(reasoning_effort_overrides or {})
        self._skip_directory_view_capture: bool = skip_directory_view_capture
        # When True, the agent gets OpenHands' native task-delegation tool so it
        # can spawn a (single-level) tree of child subagents — the naive #2 (N2)
        # baseline. Children reuse the parent's container-aware tools and lack
        # the delegation tool, so the tree is bounded to exactly two levels.
        self.enable_subagents: bool = enable_subagents
        # OpenHands binds its MCP tool-call timeout (shell_in_container etc.) as a
        # default arg at import and creates executors without passing one, so set
        # both the module constant and the already-bound default. Keeps the shell
        # timeout config-driven and identical across cells.
        self._mcp_tool_timeout_seconds: int = mcp_tool_timeout_seconds
        try:
            import inspect

            from openhands.sdk.mcp import tool as _oh_mcp_tool

            _oh_mcp_tool.MCP_TOOL_TIMEOUT_SECONDS = mcp_tool_timeout_seconds
            _init = _oh_mcp_tool.MCPToolExecutor.__init__
            if _init.__defaults__:
                _defaulted = [
                    p.name
                    for p in inspect.signature(_init).parameters.values()
                    if p.default is not inspect.Parameter.empty
                ]
                if "timeout" in _defaulted:
                    _vals = list(_init.__defaults__)
                    _vals[_defaulted.index("timeout")] = float(mcp_tool_timeout_seconds)
                    _init.__defaults__ = tuple(_vals)
        except Exception:
            logging.getLogger(__name__).warning(
                "could not apply MCP tool timeout %ss; OpenHands SDK default will apply",
                mcp_tool_timeout_seconds,
            )
        # When set, file-view/edit tool observations are captured into the shared
        # code context so downstream workers receive that source verbatim (workers
        # always run on their OWN fresh Conversation — raw Conversation reuse was
        # reverted because it overflowed the worker context window). None =
        # capture off (byte-identical to today). Injected at bootstrap.
        self._shared_code_port: SharedCodeContextProvider | None = shared_code_port

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
        children_before = snapshot_child_pids()
        capture = self._setup_shared_code_capture(agent_id, task_context)

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
                # Loop closed during shutdown; the event can no longer be
                # delivered. Debug level: fires per-event during normal teardown.
                logger.debug("Dropping OpenHands SDK event after loop shutdown")

        try:
            conversation = self._build_conversation_for_task(
                working_dir,
                mcp_servers,
                container_session,
                callbacks=[sdk_event_callback],
                prompt_cache_key=self._resolve_prompt_cache_key(task_context),
                reasoning_effort=self._resolve_reasoning_effort(task_context),
            )
        except ImportError as error:
            yield sequencer.failed(
                "OpenHands packages not installed. "
                f"Install with: uv add openhands-sdk openhands-tools. Error: {error}"
            )
            return
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {describe_error(error)}")
            return
        # One identity per built Conversation; if conversation reuse ever returns,
        # this must move with the cache so reused conversations share the id.
        conversation_identity = _conversation_identity(conversation)

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
                if thought := sdk_event_to_thought(sdk_event, sequencer):
                    yield thought
                async for captured in self._capture_file_tool(sdk_event, capture):
                    yield captured

            # Drain any callbacks that landed after the last loop check.
            while not event_queue.empty():
                sdk_event = event_queue.get_nowait()
                seen_event_ids.add(_event_dedup_key(sdk_event))
                if thought := sdk_event_to_thought(sdk_event, sequencer):
                    yield thought
                async for captured in self._capture_file_tool(sdk_event, capture):
                    yield captured

            outcome = await run_task
            shutdown_requested = outcome.shutdown_requested
            if outcome.timed_out:
                yield make_cost_recorded_event(
                    conversation,
                    sequencer,
                    tool_name=self._get_tool_name(),
                    model=self.model,
                    duration_seconds=time() - started_at,
                    container_id=container_session.container_id if container_session else None,
                    conversation_id=conversation_identity,
                    complete=False,
                    termination_reason="timeout",
                )
                yield sequencer.failed(f"Task timed out after {self.timeout_seconds} seconds")
                return

            # Fallback: yield any events that landed in ``state.events`` but never
            # came through the callback path (test fakes, SDK quirks, terminal
            # events emitted post-callback). Dedup by object identity.
            for sdk_event in iter_conversation_events(conversation):
                if _event_dedup_key(sdk_event) in seen_event_ids:
                    continue
                if thought := sdk_event_to_thought(sdk_event, sequencer):
                    yield thought
                async for captured in self._capture_file_tool(sdk_event, capture):
                    yield captured

            finish_message = extract_finish_message(conversation)
            yield make_cost_recorded_event(
                conversation,
                sequencer,
                tool_name=self._get_tool_name(),
                model=self.model,
                duration_seconds=time() - started_at,
                container_id=container_session.container_id if container_session else None,
                conversation_id=conversation_identity,
            )
            yield make_completed_event(
                conversation,
                working_dir,
                finish_message,
                sequencer,
                container_session=container_session,
            )
        except Exception as error:
            yield sequencer.failed(f"OpenHands adapter error: {describe_error(error)}")
        finally:
            if run_task is not None and not run_task.done():
                run_task.cancel()
                try:
                    await run_task
                except asyncio.CancelledError:
                    pass  # The cancellation we just requested.
                except Exception:
                    logger.exception("OpenHands run task failed while being cancelled")
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
        prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None,
    ) -> _ConversationLike:
        # Pass everything positionally so existing test fakes that monkey-patch
        # ``_build_conversation`` with ``lambda *_args: conversation`` continue to
        # work without **kwargs support.
        return cast(
            "_ConversationLike",
            self._build_conversation(
                working_dir,
                mcp_servers,
                container_session,
                callbacks,
                prompt_cache_key,
                reasoning_effort,
            ),
        )

    def _resolve_prompt_cache_key(self, task_context: dict[str, Any]) -> str | None:
        """Run-scoped OpenAI cache-shard key, or None when the feature is off."""
        if not self._run_scoped_cache_key:
            return None
        root_id = task_context.get("root_id")
        return str(root_id) if isinstance(root_id, UUID) else None

    def _resolve_reasoning_effort(self, task_context: dict[str, Any]) -> str | None:
        """First prefix-override matching the bare task, else the configured default."""
        task_summary = task_context.get("task_summary")
        if isinstance(task_summary, str):
            for prefix, effort in self._reasoning_effort_overrides.items():
                if task_summary.startswith(prefix):
                    return effort
        return self._reasoning_effort

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

    def _setup_shared_code_capture(
        self,
        agent_id: UUID,
        task_context: dict[str, Any],
    ) -> _SharedCodeCapture | None:
        """Bind a per-task shared-code capture, or None when the feature is off.

        Requires both an injected provider and a ``root_id`` in the task context
        (the run scope the block is rebuilt for). The provider is bound to a queue
        sink so captured events flow back into the worker stream.
        """
        if self._shared_code_port is None:
            return None
        root_id = task_context.get("root_id")
        if not isinstance(root_id, UUID):
            return None

        queue: asyncio.Queue[DomainEvent] = asyncio.Queue()

        async def _enqueue(_agent_id: UUID, events: list[DomainEvent]) -> None:
            for event in events:
                queue.put_nowait(event)

        bound = self._shared_code_port.bind_capture(_enqueue)
        return _SharedCodeCapture(port=bound, root_id=root_id, agent_id=agent_id, queue=queue)

    async def _capture_file_tool(
        self,
        sdk_event: Any,
        capture: _SharedCodeCapture | None,
    ) -> AsyncIterator[DomainEvent]:
        """Forward a file-editor observation to the shared-code port, yield its events.

        A full-file ``view`` records the returned content (the byte source for the
        shared block); a mutating command marks the file edited so its recorded
        content is invalidated until re-viewed. Best-effort: a capture failure must
        never break the worker run, so errors are logged and swallowed.
        """
        if capture is None:
            return
        # A ``view`` action carries the requested ``view_range``; its observation
        # arrives as a later event. Stash the range so the observation can be
        # recorded as a full read (no range) or an excerpt (a slice) truthfully.
        self._track_view_action(getattr(sdk_event, "action", None), capture)
        observation = getattr(sdk_event, "observation", None)
        if observation is None:
            return
        command = getattr(observation, "command", None)
        path = getattr(observation, "path", None)
        if not isinstance(command, str) or not isinstance(path, str) or not path:
            return

        try:
            if command in _FILE_VIEW_COMMANDS:
                content = observation_text(observation)
                requested = capture.pending_view_ranges.pop(path, None)
                if content and not self._is_skipped_directory_listing(content):
                    capture_type, req_start, req_end, act_start, act_end = _classify_view_capture(
                        content, requested
                    )
                    await capture.port.record_view(
                        capture.root_id,
                        capture.agent_id,
                        path,
                        content,
                        capture_type=capture_type,
                        requested_start_line=req_start,
                        requested_end_line=req_end,
                        actual_start_line=act_start,
                        actual_end_line=act_end,
                        role="worker",
                    )
            elif command in _FILE_EDIT_COMMANDS:
                await capture.port.record_edit(capture.root_id, capture.agent_id, path)
        except Exception:
            logger.warning(
                "shared-code capture failed for %s on %s", command, path, exc_info=True
            )

        while not capture.queue.empty():
            yield capture.queue.get_nowait()

    @staticmethod
    def _track_view_action(action: Any, capture: _SharedCodeCapture) -> None:
        """Remember a ``view`` action's requested range until its observation lands."""
        if action is None:
            return
        if getattr(action, "command", None) != "view":
            return
        path = getattr(action, "path", None)
        if isinstance(path, str) and path:
            capture.pending_view_ranges[path] = getattr(action, "view_range", None)

    def _is_skipped_directory_listing(self, content: str) -> bool:
        return self._skip_directory_view_capture and content.lstrip().startswith(
            _DIRECTORY_LISTING_VIEW_PREFIX
        )

    def _make_completed_event(
        self,
        conversation: _ConversationLike,
        working_dir: str,
        finish_message: str | None,
        sequencer: EventSequencer,
        container_session: ContainerSessionContext | None = None,
    ) -> DomainEvent:
        return make_completed_event(
            conversation, working_dir, finish_message, sequencer, container_session
        )

    def _build_conversation(
        self,
        working_dir: str,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        container_session: ContainerSessionContext | None = None,
        callbacks: list[Callable[[Any], None]] | None = None,
        prompt_cache_key: str | None = None,
        reasoning_effort: str | None = None,
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

        llm = LLM(**self._build_llm_kwargs(reasoning_effort))
        if prompt_cache_key is not None:
            # Pre-pin before Conversation.__init__ runs _pin_prompt_cache_key,
            # which only assigns when the key is still None. A run-scoped key
            # puts all of the run's workers on one OpenAI prefix-cache shard.
            llm._prompt_cache_key = prompt_cache_key  # noqa: SLF001 - SDK PrivateAttr, no public setter
        tools = self._build_native_tools(Tool, container_session)
        # Agent creates MCP tool definitions from this config after native tools.
        mcp_config = to_openhands_mcp_config(mcp_servers) if mcp_servers else {}

        if self.enable_subagents:
            self._register_container_subagent(Tool, Agent, container_session, mcp_config)
            tools = [*tools, Tool(name="task_tool_set")]

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

    def _build_llm_kwargs(self, reasoning_effort: str | None = None) -> dict[str, Any]:
        # caching_prompt enables Anthropic prompt caching in the OpenHands SDK.
        # It is the SDK default, but set explicitly here so the intent is
        # visible; the SDK gates it by model support, so it is a safe no-op for
        # non-Anthropic models.
        llm_kwargs: dict[str, Any] = {
            "model": self.model,
            "api_key": self.api_key,
            "caching_prompt": True,
        }
        if reasoning_effort is not None:
            # SDK default is "high" — the dominant completion-token cost on
            # reasoning models (gpt-5.x routes via the Responses API, which
            # always sends reasoning.effort).
            llm_kwargs["reasoning_effort"] = reasoning_effort
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

    def _register_container_subagent(
        self,
        tool_factory: Callable[..., Any],
        agent_factory: Callable[..., Any],
        container_session: ContainerSessionContext | None,
        mcp_config: dict[str, Any],
    ) -> None:
        """Register a container-aware ``general-purpose`` child agent for N2.

        OpenHands' native ``task_tool_set`` delegates to a registered agent
        type (default ``general-purpose``). The SDK's built-in general-purpose
        agent runs on the HOST, so its shell/build commands would diverge from
        the CVE container. We instead register a child whose tools are the
        parent's own container-aware {file_editor, glob, grep} plus the same
        MCP ``shell_in_container`` server, so subagent work lands in the
        container. Children get no delegation tool, bounding the tree to two
        levels. ``register_agent_if_absent`` is idempotent; the naive runner
        executes one job per process, so the first registration wins.
        """
        from openhands.sdk.subagent.registry import register_agent_if_absent

        def _build_child(llm: Any) -> Any:
            child_tools = self._build_native_tools(tool_factory, container_session)
            return agent_factory(
                llm=llm,
                tools=child_tools,
                mcp_config=mcp_config,
                include_default_tools=_OPENHANDS_DEFAULT_CONTROL_TOOLS,
            )

        register_agent_if_absent(
            "general-purpose",
            _build_child,
            "Container-aware general-purpose subagent for SEC-bench BEF tasks.",
        )

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

    def _extract_result(
        self,
        conversation: Any,
        working_dir: str,
        finish_message: str | None = None,
    ) -> str:
        return extract_result(conversation, working_dir, finish_message)

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
                timeout=(float(SHUTDOWN_GRACE_SECONDS) * 2) + 1.0,
            )
        except TimeoutError:
            logger.warning(
                "OpenHands shutdown did not finish within %.0fs; cleanup will "
                "continue in its background thread",
                (float(SHUTDOWN_GRACE_SECONDS) * 2) + 1.0,
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
        unconditionally. ``pid_alive`` short-circuits the kill when the SDK
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
        reap_with_escalation(set(mcp_child_pids))

    @staticmethod
    def _classify_event(event: Any) -> str:
        return classify_event(event)

    @staticmethod
    def _extract_event_content(event: Any) -> str | None:
        return extract_event_content(event)
