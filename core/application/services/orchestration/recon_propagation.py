"""Recon-read and shared-source propagation for the orchestrator.

Two independent propagation paths, both fed by tool-calling recon results:

* **Shared code block** (worker tier): rebuilt from the event store by the
  injected ``SharedCodeContextPort`` and injected into worker prompts. Manager
  ``read_file`` results are optionally persisted as ``SourceFileObserved`` so
  downstream workers inherit them (gated by ``capture_recon_reads``).
* **Boss recon block** (orchestration tier): the BOSS's reads rendered into a
  block held in an in-memory per-run map and injected into MANAGER decomposition
  prompts only — it never reaches the worker-consumed shared block.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal, cast

from core.domain.values.enums import AgentRole


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any
    from uuid import UUID

    from core.domain.aggregates.agent_session import AgentSession
    from core.ports.shared_code_context_port import ContextPacket, SharedCodeContextPort

logger = logging.getLogger(__name__)

# recon read_file frames a targeted range read with a "lines S-E of TOTAL" header
# and appends this marker when a rangeless read overflows its max_lines cap
# (see infrastructure/adapters/recon_tool_adapter.read_file).
_RECON_RANGE_HEADER = re.compile(r"lines (\d+)-(\d+) of \d+")
_RECON_TRUNCATION_MARKER = "[...truncated,"


class ReconPropagationService:
    """Owns shared-code-block resolution and the per-run boss recon map."""

    def __init__(
        self,
        shared_code_port: SharedCodeContextPort | None = None,
        capture_recon_reads: bool = False,
        share_boss_recon: bool = False,
    ) -> None:
        # When set, the shared code-prefix block (source upstream workers already
        # read, rebuilt from events) is injected into the worker prompt's stable
        # prefix. None => the block is never populated (byte-identical to today).
        self._shared_code_port = shared_code_port
        # When True (and a shared-code port is present), a manager's recon
        # read_file results are persisted as SourceFileObserved on its own
        # aggregate, so the run's block carries them to downstream workers
        # (read-once → propagate). Off => recon reads are not captured.
        self._capture_recon_reads = capture_recon_reads
        # When True, the BOSS's recon read_file results are rendered into a block
        # injected into MANAGER decomposition prompts only (orchestration tier),
        # so managers inherit the boss's reads instead of re-reading. Kept in an
        # in-memory per-run store — it never reaches the shared block workers
        # consume, so the worker tier is unaffected by construction.
        self._share_boss_recon = share_boss_recon
        self._boss_recon_blocks: dict[Any, str] = {}

    async def resolve_shared_code_block(self, root_id: UUID | None) -> str | None:
        """The run's shared code-prefix block, or None when the feature is off.

        Rebuilt from the event store by the provider; gated by the presence of the
        port (only wired when ``orchestration.shared_worker_session`` is on) and a
        known run scope.
        """
        if self._shared_code_port is None or root_id is None:
            return None
        return await self._shared_code_port.code_block(root_id)

    async def resolve_shared_code_index(self, root_id: UUID | None) -> str | None:
        """Compact index of the shared block's files, or None when disabled.

        Rendered near the <task> block (volatile tail) so the model sees what is
        already provided in full right where it decides which files to read.
        """
        if self._shared_code_port is None or root_id is None:
            return None
        return await self._shared_code_port.code_index(root_id)

    async def resolve_context_packet(
        self,
        root_id: UUID | None,
        *,
        target_paths: tuple[str, ...],
        symbols: tuple[str, ...],
        failure_digest: str | None,
        hard_predecessor_ids: tuple[UUID, ...] = (),
    ) -> ContextPacket | None:
        """Resolve scoped packets, falling back to the legacy provider contract."""
        if self._shared_code_port is None or root_id is None:
            return None
        assembler = getattr(self._shared_code_port, "context_packet", None)
        if callable(assembler):
            typed_assembler = cast(
                "Callable[..., Awaitable[ContextPacket]]",
                assembler,
            )
            return await typed_assembler(
                root_id,
                target_paths=target_paths,
                symbols=symbols,
                failure_digest=failure_digest,
                hard_predecessor_ids=hard_predecessor_ids,
            )
        from core.ports.shared_code_context_port import ContextPacket

        block = await self._shared_code_port.code_block(root_id)
        index = await self._shared_code_port.code_index(root_id)
        text = (block or "") + (index or "")
        return ContextPacket(
            source_block=block,
            source_index=index,
            total_chars=len(text),
            total_tokens=(len(text) + 3) // 4,
        )

    def capture_source_reads(self, agent: AgentSession, captured_reads: list) -> None:
        """Persist a manager's recon read_file results as SourceFileObserved.

        Gated on ``capture_recon_reads``: the in-loop verbatim/dedup of source
        reads is always on (manager-side, worker-neutral), but PERSISTING them
        into the run's shared block carries them to every downstream worker and
        inflates worker prompts — measured net-negative for the worker tier, so
        it is off by default and decoupled from the cache optimization.

        The events land on the manager's own in-hand aggregate (sequenced with
        the decomposition operation — no out-of-band append). One event per
        distinct path: a recon loop re-reading a file returns identical content,
        so the first capture is sufficient and avoids aggregate bloat.
        """
        if not self._capture_recon_reads or not captured_reads or self._shared_code_port is None:
            return
        agent_role = getattr(agent, "role", None)
        role = agent_role.value if agent_role is not None else ""
        seen: set[str] = set()
        for read in captured_reads:
            path = read.arguments.get("path")
            if not isinstance(path, str) or not path or path in seen:
                continue
            if not read.content:
                continue
            seen.add(path)
            capture_type, req_start, req_end, act_start, act_end = self._classify_recon_read(read)
            agent.record_source_file_observed(
                path=path,
                content=read.content,
                observed_by=agent.agent_id,
                capture_type=capture_type,
                requested_start_line=req_start,
                requested_end_line=req_end,
                actual_start_line=act_start,
                actual_end_line=act_end,
                phase="recon",
                role=role,
            )

    @staticmethod
    def _classify_recon_read(
        read: Any,
    ) -> tuple[
        Literal["full", "range", "truncated"], int | None, int | None, int | None, int | None
    ]:
        """Classify a recon read_file result truthfully (Bug 1: never blanket 'full').

        A read carrying start_line/end_line is a slice (``range``); a rangeless read
        clipped at its max_lines cap is ``truncated``; only a whole-file read is
        ``full``. Returns ``(capture_type, requested_start, requested_end,
        actual_start, actual_end)`` so an excerpt is never later shown as a full file.
        """
        arguments = getattr(read, "arguments", None) or {}
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        content = getattr(read, "content", "") or ""
        if isinstance(start, int) or isinstance(end, int):
            match = _RECON_RANGE_HEADER.search(content)
            actual_start = int(match.group(1)) if match else start
            actual_end = int(match.group(2)) if match else end
            return "range", start, end, actual_start, actual_end
        if _RECON_TRUNCATION_MARKER in content:
            return "truncated", None, None, 1, None
        return "full", None, None, None, None

    def store_boss_recon_block(self, agent: AgentSession, captured_reads: list) -> None:
        """Render the BOSS's recon reads into a block for the run's managers.

        Manager-only propagation: the block is held in an in-memory per-run map
        and injected into manager decomposition prompts; it never enters the
        shared block workers consume, so worker cost is unaffected.
        """
        if not self._share_boss_recon or agent.role != AgentRole.BOSS or not captured_reads:
            return
        block = self.render_boss_recon_block(captured_reads)
        if block:
            self._boss_recon_blocks[self._run_scope(agent)] = block

    @staticmethod
    def render_boss_recon_block(captured_reads: list) -> str | None:
        """A <provided_source_files> block from the boss's reads (one per path)."""
        latest: dict[str, str] = {}
        for read in captured_reads:
            path = read.arguments.get("path")
            if isinstance(path, str) and path and read.content:
                latest[path] = read.content
        if not latest:
            return None
        parts = [
            "<provided_source_files>",
            "    Source files the BOSS already read while planning this task, provided IN "
            "FULL. Build on them and do NOT re-read a file shown here unless you edit it.",
        ]
        for path, content in latest.items():
            parts.append(f'<file path="{path}">\n{content}\n</file>')
        parts.append("</provided_source_files>")
        return "\n".join(parts)

    @staticmethod
    def _run_scope(agent: AgentSession) -> Any:
        return agent.hierarchy_limits.root_id if agent.hierarchy_limits else agent.agent_id

    def resolve_boss_recon_block(self, agent: AgentSession) -> str | None:
        if not self._share_boss_recon:
            return None
        return self._boss_recon_blocks.get(self._run_scope(agent))
