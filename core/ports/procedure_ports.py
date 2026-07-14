"""Port for the deterministic procedure execution tier.

Tasks that are fixed procedures (domain-registered) execute host-side with
zero LLM turns. Core sees only this Protocol; a domain plugin provides the
registry and execution; the null default disables the tier.
"""

from __future__ import annotations

from typing import Any, Protocol

from core.domain.values.procedure import ProcedureResult


class ProcedureExecutorPort(Protocol):
    """Match, validate, and execute registered deterministic procedures."""

    def match(self, task_description: str, domain_context: object | None) -> str | None:
        """Return a procedure_ref when the task maps to a registered procedure."""
        ...

    def resolve(self, procedure_ref: str) -> bool:
        """True iff a Host-matched ref still names a registered procedure."""
        ...

    async def execute(
        self,
        procedure_ref: str,
        task_description: str,
        domain_context: object | None,
        params: dict[str, Any],
    ) -> ProcedureResult:
        """Run the procedure.

        Task-level failure returns ``success=False`` with a digest; raises
        only for infrastructure faults (the caller converts those to a
        failed result and escalates agentically).
        """
        ...


class NullProcedureExecutor:
    """Default binding when no domain plugin provides procedures."""

    def match(self, task_description: str, domain_context: object | None) -> str | None:
        return None

    def resolve(self, procedure_ref: str) -> bool:
        return False

    async def execute(
        self,
        procedure_ref: str,
        task_description: str,
        domain_context: object | None,
        params: dict[str, Any],
    ) -> ProcedureResult:
        raise RuntimeError("No procedure executor bound; dispatch must gate on resolve()")
