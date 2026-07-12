"""Worker cost knobs on the OpenHands adapter.

Three independent levers, all default-off / default-None so the adapter is
byte-identical to today unless configured:

* run-scoped prompt_cache_key — pre-pin the SDK's OpenAI cache key to the run
  root id so every worker in a run shares one prefix-cache shard (the SDK
  otherwise pins per-conversation, defeating cross-worker reuse);
* reasoning_effort (+ task-prefix overrides) — the SDK default is "high", the
  dominant completion-token cost on reasoning models;
* directory-listing capture skip — keep ``view``-on-directory snapshots out of
  the shared code block.

Plus the result-path sanitization: transcript tails that bypass the tool-layer
path mapper must not leak host paths into ``agent.result`` (and from there into
sibling prompts).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.domain.values.node_message import WORKER_CONCLUSION_MARKER
from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter
from infrastructure.adapters.worker.shared import EventSequencer
from infrastructure.tests.test_openhands_adapter import _container_session, _FakeConversation


def _adapter(**kwargs: Any) -> OpenHandsAdapter:
    return OpenHandsAdapter(model="openai/gpt-4o", **kwargs)


class TestPromptCacheKey:
    def test_resolves_run_scoped_key_from_root_id(self) -> None:
        # Given: the flag on and a root_id in the task context
        adapter = _adapter(run_scoped_cache_key=True)
        root_id = uuid4()

        # When
        key = adapter._resolve_prompt_cache_key({"root_id": root_id})

        # Then: the run id is the shard key
        assert key == str(root_id)

    def test_flag_off_resolves_none(self) -> None:
        # Given: default construction (flag off)
        adapter = _adapter()

        # When / Then
        assert adapter._resolve_prompt_cache_key({"root_id": uuid4()}) is None

    def test_missing_root_id_resolves_none(self) -> None:
        # Given: flag on but no run scope in the context
        adapter = _adapter(run_scoped_cache_key=True)

        # When / Then
        assert adapter._resolve_prompt_cache_key({}) is None

    def test_sdk_llm_accepts_preset_private_cache_key(self) -> None:
        # Given: a real SDK LLM built from the adapter's kwargs
        from openhands.sdk import LLM

        adapter = _adapter(api_key="test-key")
        llm = LLM(**adapter._build_llm_kwargs())

        # When: pre-pinning the key the way _build_conversation does
        llm._prompt_cache_key = "run-1234"

        # Then: the PrivateAttr holds (Conversation's pin only assigns when None)
        assert llm._prompt_cache_key == "run-1234"


class TestReasoningEffort:
    def test_kwargs_omit_effort_when_unset(self) -> None:
        # Given: default construction
        adapter = _adapter()

        # When
        kwargs = adapter._build_llm_kwargs(None)

        # Then: SDK default ("high") stays in charge
        assert "reasoning_effort" not in kwargs

    def test_kwargs_carry_effort_and_sdk_accepts_it(self) -> None:
        # Given
        from openhands.sdk import LLM

        adapter = _adapter(api_key="test-key")

        # When
        kwargs = adapter._build_llm_kwargs("low")
        llm = LLM(**kwargs)

        # Then
        assert kwargs["reasoning_effort"] == "low"
        assert llm.reasoning_effort == "low"

    def test_override_matches_task_prefix(self) -> None:
        # Given: a default effort and a per-branch override
        adapter = _adapter(
            reasoning_effort="low",
            reasoning_effort_overrides={"[Exploiter]": "medium"},
        )

        # When / Then: first matching prefix wins; otherwise the default
        assert (
            adapter._resolve_reasoning_effort({"task_summary": "[Exploiter] build a PoC"})
            == "medium"
        )
        assert (
            adapter._resolve_reasoning_effort({"task_summary": "[Builder] compile it"}) == "low"
        )
        assert adapter._resolve_reasoning_effort({}) == "low"

    def test_unconfigured_resolves_none(self) -> None:
        # Given: nothing configured
        adapter = _adapter()

        # When / Then: None = keep the SDK default
        assert adapter._resolve_reasoning_effort({"task_summary": "[Builder] x"}) is None


class _SpySharedCodePort:
    """Records record_view calls; bind_capture returns itself with a sink."""

    def __init__(self) -> None:
        self.views: list[str] = []

    async def record_view(
        self,
        root_id: UUID,
        agent_id: UUID,
        path: str,
        content: str,
        **capture: Any,
    ) -> None:
        self.views.append(path)

    async def record_edit(self, root_id: UUID, agent_id: UUID, path: str) -> None:
        return None

    def bind_capture(self, emit_sink: Any) -> "_SpySharedCodePort":
        return self

    async def code_block(self, root_id: UUID) -> str | None:  # pragma: no cover - unused
        return None

    async def code_index(self, root_id: UUID) -> str | None:  # pragma: no cover - unused
        return None


def _view_event(path: str, text: str) -> Any:
    observation = SimpleNamespace(
        command="view",
        path=path,
        content=[SimpleNamespace(text=text)],
        metadata=None,
    )
    return SimpleNamespace(observation=observation, action=None, id=uuid4())


_DIR_LISTING = (
    "Here's the files and directories up to 2 levels deep in /testcase, "
    "excluding hidden items:\n/testcase/a\n/testcase/b"
)


class TestDirectoryListingCaptureSkip:
    @pytest.mark.asyncio
    async def test_skips_directory_listing_when_enabled(self) -> None:
        # Given: skip flag on and a worker viewing a directory then a file
        port = _SpySharedCodePort()
        adapter = _adapter(shared_code_port=port, skip_directory_view_capture=True)  # type: ignore[arg-type]
        capture = adapter._setup_shared_code_capture(uuid4(), {"root_id": uuid4()})
        assert capture is not None

        # When
        async for _ in adapter._capture_file_tool(_view_event("/testcase", _DIR_LISTING), capture):
            pass
        async for _ in adapter._capture_file_tool(_view_event("/src/a.c", "int x;"), capture):
            pass

        # Then: only the file view is captured
        assert port.views == ["/src/a.c"]

    @pytest.mark.asyncio
    async def test_captures_directory_listing_when_disabled(self) -> None:
        # Given: default construction (skip flag off)
        port = _SpySharedCodePort()
        adapter = _adapter(shared_code_port=port)  # type: ignore[arg-type]
        capture = adapter._setup_shared_code_capture(uuid4(), {"root_id": uuid4()})
        assert capture is not None

        # When
        async for _ in adapter._capture_file_tool(_view_event("/testcase", _DIR_LISTING), capture):
            pass

        # Then: legacy behavior — directory snapshots are captured
        assert port.views == ["/testcase"]


class TestCompletedResultSanitization:
    def test_host_paths_in_result_are_mapped_to_container_paths(self, tmp_path: Path) -> None:
        # Given: a finish message that leaked a host workspace path
        adapter = _adapter()
        session = _container_session(tmp_path)
        host_report = f"{tmp_path / 'testcase' / 'report.md'}"
        conversation = _FakeConversation()

        # When
        event = adapter._make_completed_event(
            conversation,  # type: ignore[arg-type]
            working_dir=str(tmp_path),
            finish_message=f"Wrote {host_report}",
            sequencer=EventSequencer(uuid4(), stream="openhands"),
            container_session=session,
        )

        # Then: the result speaks container paths, never host paths
        assert "/testcase/report.md" in event.result
        assert str(tmp_path) not in event.result
        assert WORKER_CONCLUSION_MARKER in event.result
