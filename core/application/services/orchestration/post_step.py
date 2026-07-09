"""Post-step handling for a completed ``run_agent_step``.

After an agent's events are persisted, ``PostStepHandler`` spawns any newly
declared children, propagates completion/failure to the parent, records a
deterministic crash digest, and drives the worker/verification retry ladder.
It also owns the failure path when a step raises before persistence.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.application.services.orchestration.failure_digest import build_failure_digest
from core.domain.aggregates.agent_session import AgentRole, AgentStatus
from core.domain.events.events import ChildSpawned
from core.domain.services.context_update_parser import parse_context_update


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from uuid import UUID

    from core.application.services.lifecycle.agent_repository import AgentRepository
    from core.application.services.lifecycle.child_factory import ChildAgentFactory
    from core.application.services.lifecycle.hierarchy_limits_registry import (
        HierarchyLimitsRegistry,
    )
    from core.application.services.lifecycle.parent_notifier import (
        ParentNotificationService,
    )
    from core.application.services.orchestration.retry_policy import RetryPolicy
    from core.application.types import ProgressCallback
    from core.domain.aggregates.agent_session import AgentSession
    from core.domain.events.events import DomainEvent
    from core.ports.runtime_ports import SharedContextPort


class PostStepHandler:
    """Handle child spawning, parent notification, and retries after a step."""

    def __init__(
        self,
        *,
        repository: AgentRepository,
        child_factory: ChildAgentFactory,
        parent_notifier: ParentNotificationService,
        retry_policy: RetryPolicy,
        shared_context_port: SharedContextPort,
        limits_registry: HierarchyLimitsRegistry,
        verification_max_retries: int,
        progress_callback: ProgressCallback | None,
    ) -> None:
        self._repository = repository
        self._child_factory = child_factory
        self._parent_notifier = parent_notifier
        self._retry_policy = retry_policy
        self._shared_context_port = shared_context_port
        self._limits_registry = limits_registry
        self._verification_max_retries = verification_max_retries
        self._progress_callback = progress_callback

    async def handle_post_step(self, agent: AgentSession, events: list[DomainEvent]) -> None:
        child_events = [e for e in events if isinstance(e, ChildSpawned)]
        if child_events:
            await self._child_factory.create_children_from_events(child_events, agent.agent_id)
            # The factory's ``commit_reservation`` has now moved the
            # reserved slots into ``_total_created`` for these children;
            # clear the in-memory marker so a later failure path on the
            # same aggregate cannot double-release the slot count.
            agent.pending_reservation = 0

        if agent.role == AgentRole.WORKER and agent.status == AgentStatus.COMPLETED:
            await self._process_worker_context_updates(agent)

        if agent.status == AgentStatus.COMPLETED:
            await self._parent_notifier.notify_if_complete(agent)
            return

        if agent.status == AgentStatus.FAILED:
            # A crashed worker (no verification feedback) otherwise carries no
            # retry context. Capture a deterministic digest of the failed attempt
            # so the retry — and, on propagation, the parent — sees why it failed.
            # Best-effort: a digest-build error must never block the failure path.
            if (
                agent.role == AgentRole.WORKER
                and not agent.verification_feedback
                and agent.failure_digest is None
            ):
                try:
                    digest = build_failure_digest(agent)
                except Exception:
                    logger.warning(
                        "Failed to build failure digest for agent %s",
                        agent.agent_id,
                        exc_info=True,
                    )
                else:
                    agent.record_failure_digest(digest, source="worker_crash")
                    await self._repository.persist_events(agent, self._progress_callback)

            # Escalation ladder: a failed procedural attempt gets exactly one
            # guaranteed agentic retry (dispatch routes it agentic via
            # last_attempt_procedural), carrying the procedure's digest.
            if (
                agent.role == AgentRole.WORKER
                and agent.last_attempt_procedural
                and agent.retry_count == 0
            ):
                agent.schedule_retry(reason=f"Procedural attempt failed: {agent.error_message}")
                await self._repository.persist_events(agent, self._progress_callback)
                return

            # Worker retry: up to verification_max_retries with judge feedback or
            # crash digest as the injected context.
            if await self._maybe_retry_worker(agent):
                return

            retried = await self._retry_policy.maybe_schedule_retry(agent)
            if retried:
                return  # Agent re-queued for execution, don't notify parent

            await self._parent_notifier.notify_if_failed(agent)

    async def _maybe_retry_worker(self, agent: AgentSession) -> bool:
        """Retry a worker that failed verification or crashed, with feedback/digest context.

        Returns True if a retry was scheduled (caller should not notify parent).
        """
        if agent.role != AgentRole.WORKER:
            return False

        if not (agent.verification_feedback or agent.failure_digest):
            return False

        if agent.retry_count >= self._verification_max_retries:
            return False

        reason = agent.error_message or "Verification failed"
        agent.schedule_retry(reason=reason, escalated_model=None)

        await self._repository.persist_events(agent, self._progress_callback)
        logger.info(
            "Worker retry %d/%d for agent %s: %s",
            agent.retry_count,
            self._verification_max_retries,
            agent.agent_id,
            agent.verification_feedback or agent.failure_digest,
        )
        return True

    async def _process_worker_context_updates(self, agent: AgentSession) -> None:
        if agent.result is None:
            return

        parsed = parse_context_update(agent.result)
        if parsed is None:
            return

        root_id = self._limits_registry.get_root_id(agent.agent_id)
        context = await self._shared_context_port.get(root_id)
        if context is None:
            return

        current_version = context.version

        for decision in parsed.decisions:
            context.record_decision(
                decision_key=decision.key,
                decision_value=decision.value,
                rationale=decision.rationale,
                decided_by=agent.agent_id,
            )

        for output in parsed.artifacts:
            context.store_artifact(
                key=output.key,
                content_type="text/plain",
                stored_by=agent.agent_id,
                content=output.description,
            )

        if context.events:
            await self._shared_context_port.save(context, expected_version=current_version)
            context.mark_changes_as_committed()

    async def handle_step_failure(self, agent_id: UUID, error: Exception) -> None:
        try:
            agent = await self._repository.load_if_exists(agent_id)
            if agent is None:
                return

            agent.fail_with_reason(f"Execution error: {error!r}")
            await self._repository.persist_events(agent, self._progress_callback)

            await self._parent_notifier.notify_if_failed(agent)
        except Exception as persist_error:
            raise RuntimeError(
                f"Failed to persist error state for agent {agent_id}: {persist_error!r}"
            ) from error
