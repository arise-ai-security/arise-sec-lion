"""Per-call recon policy for the tool-calling loop.

Controls how aggressively a PENDING/MANAGER agent explores the codebase
during reconnaissance. Resolved from config per (role, domain) pair and
passed to ``ToolCallingService.run_with_tools()`` on each invocation.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconPolicy:
    """Per-call policy governing the recon tool-calling loop.

    Resolved by ``AgentOrchestrator._resolve_recon_policy(role, domain)``
    from the ``orchestration.recon`` config section.
    """

    enabled: bool = True
    max_iterations: int = 5
    result_char_limit: int = 6_000
    allowed_tools: frozenset[str] | None = None  # None = all tools
