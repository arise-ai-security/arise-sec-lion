"""Procedure execution result values for the deterministic worker tier."""

from pydantic import BaseModel


class ProcedureEvidence(BaseModel):
    """Host-captured record of one procedure command (agent-unforgeable)."""

    model_config = {"frozen": True}

    argv: tuple[str, ...]
    exit_code: int
    output_sha256: str
    excerpt: str = ""


class ProcedureResult(BaseModel):
    """Outcome of a deterministic procedure run.

    ``summary`` becomes WorkCompleted.result on success; ``digest`` carries
    bounded failure context for the agentic escalation on failure.
    """

    model_config = {"frozen": True}

    success: bool
    summary: str
    digest: str = ""
    evidence: tuple[ProcedureEvidence, ...] = ()
