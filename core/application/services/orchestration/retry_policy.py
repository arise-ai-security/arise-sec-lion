"""Retry policy for failed worker agents.

Owns retry state, model escalation, and circuit-breaker decisions.
"""

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

    def reset(self) -> None:
        """Clear per-run retry state."""
        self._model_failures.clear()

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
            self._model_failures[current_model] = (
                self._model_failures.get(current_model, 0) + 1
            )

        reason = agent.error_message or "Unknown failure"
        agent.schedule_retry(reason=reason, escalated_model=escalated_model)

        await self._repository.persist_events(
            agent,
            self._progress_callback,
        )
        return True

    def _max_retries(self) -> int:
        """Retry budget is the length of the escalation chain."""
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
        failures = self._model_failures.get(model, 0)
        return failures >= retry_cfg.circuit_breaker_threshold

    def _get_escalated_model(self, agent: AgentSession) -> str | None:
        chain = self._get_model_chain()
        if not chain:
            return None

        current_model = self._get_current_model(agent)
        candidates = (
            chain[chain.index(current_model) + 1 :]
            if current_model in chain
            else chain
        )

        for model in candidates:
            if not self._is_circuit_broken(model):
                return model
        return None

    @staticmethod
    def _get_current_model(agent: AgentSession) -> str | None:
        return agent.config.base.model
