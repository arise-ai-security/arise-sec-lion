"""Retry policy for failed worker agents.

Owns retry state, model escalation, and circuit-breaker decisions.
"""

import time
from typing import Any

from core.application.types import ProgressCallback
from core.domain.aggregates.agent_session import AgentRole, AgentSession

from core.application.services.lifecycle.agent_repository import AgentRepository


class RetryPolicy:
    """Decide whether failed workers should be retried and persist retries."""

    def __init__(
        self,
        repository: AgentRepository,
        retry_config: Any = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self._repository = repository
        self._retry_config = retry_config
        self._progress_callback = progress_callback
        self._model_failures: dict[str, int] = {}
        self._model_last_failure_time: dict[str, float] = {}

    def reset(self) -> None:
        """Clear per-run retry state."""
        self._model_failures.clear()
        self._model_last_failure_time.clear()

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """Set the progress callback for retry persistence."""
        self._progress_callback = callback

    async def maybe_schedule_retry(self, agent: AgentSession) -> bool:
        """Attempt to retry a failed agent.

        Returns True when a retry was scheduled and persisted.
        """
        if agent.role != AgentRole.WORKER:
            return False

        max_retries = self._max_retries()
        if max_retries == 0 or agent.retry_count >= max_retries:
            return False

        current_model = self._get_current_model(agent)
        escalated_model = self._get_escalated_model(agent)

        if current_model and self._is_circuit_broken(current_model) and escalated_model is None:
            return False

        if current_model:
            now = time.monotonic()
            self._model_failures[current_model] = (
                self._model_failures.get(current_model, 0) + 1
            )
            self._model_last_failure_time[current_model] = now

        reason = agent.error_message or "Unknown failure"
        agent.schedule_retry(reason=reason, escalated_model=escalated_model)

        await self._repository.persist_events(
            agent,
            agent.version - 1,
            self._progress_callback,
        )
        return True

    def _max_retries(self) -> int:
        """Return the configured worker retry budget."""
        retry_cfg = self._retry_config
        if retry_cfg is not None and retry_cfg.max_worker_retries is not None:
            return retry_cfg.max_worker_retries
        return len(self._get_model_chain())

    def _get_model_chain(self) -> list[str]:
        retry_cfg = self._retry_config
        if retry_cfg is None or not retry_cfg.model_escalation_chain:
            return []
        return list(retry_cfg.model_escalation_chain)

    def _is_circuit_broken(self, model: str) -> bool:
        """Check if a model has tripped the circuit breaker."""
        retry_cfg = self._retry_config
        if retry_cfg is None:
            return False
        self._maybe_reset_circuit_breaker(model)
        failures = self._model_failures.get(model, 0)
        return failures >= retry_cfg.circuit_breaker_threshold

    def _maybe_reset_circuit_breaker(self, model: str) -> None:
        """Reset stale circuit-breaker state for a model after the cooldown window."""
        retry_cfg = self._retry_config
        if retry_cfg is None or retry_cfg.circuit_breaker_reset_seconds <= 0:
            return

        last_failure = self._model_last_failure_time.get(model)
        if last_failure is None:
            return

        if time.monotonic() - last_failure < retry_cfg.circuit_breaker_reset_seconds:
            return

        self._model_failures.pop(model, None)
        self._model_last_failure_time.pop(model, None)

    def _get_escalated_model(self, agent: AgentSession) -> str | None:
        """Get the next healthy retry model for this attempt."""
        chain = self._get_model_chain()
        if not chain:
            return None

        start_index = min(agent.retry_count, len(chain) - 1)
        for model in chain[start_index:]:
            if not self._is_circuit_broken(model):
                return model
        return None

    @staticmethod
    def _get_current_model(agent: AgentSession) -> str | None:
        base = getattr(agent.config, "base", None)
        return getattr(base, "model", None)
