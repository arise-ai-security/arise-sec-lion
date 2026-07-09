"""Container-aware OpenHands native tools and host<->container path mapping.

Registers path-mapping wrappers around the OpenHands ``file_editor`` / ``glob`` /
``grep`` native tools so a run's host mirror directories stay invisible to the
agent (it only ever sees canonical container paths), and rewrites host paths out
of tool observations and transcript tails. Registration is process-global and
idempotent (guarded by a lock), matching the SDK's global tool registry.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from .shared import ContainerSessionContext


logger = logging.getLogger(__name__)

_CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED = False
_CONTAINER_AWARE_OPENHANDS_TOOLS_LOCK = threading.Lock()


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


def replace_host_paths(
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

    container_path = replace_host_paths(path, container_session)
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


def _ensure_container_aware_openhands_tools_registered() -> None:
    global _CONTAINER_AWARE_OPENHANDS_TOOLS_REGISTERED  # noqa: PLW0603 - process-global tool registry flag
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
