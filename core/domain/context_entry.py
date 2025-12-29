"""Context entry for cross-session knowledge sharing."""

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field

from core.domain.subtask import WorkerReport


class ContextEntryType(str, Enum):
    """Type of context entry in the dashboard."""

    WORKER = "worker"  # Context from completed worker task
    SOURCE = "source"  # Key information extracted from Boss prompt


class SourceContextData(BaseModel):
    """Structured data extracted from the Boss's original prompt.

    Contains key information that workers may need to reference:
    - Bug reports with details
    - Commit/version references
    - File/script paths
    - Error messages and stack traces
    - Environment details
    - Reproduction steps
    - Security build context (dockerfile, build_sh, etc.)
    """

    model_config = {"frozen": True}

    # Bug/issue information
    bug_summary: str = Field("", description="Summary of the bug or issue")
    error_messages: list[str] = Field(
        default_factory=list,
        description="Error messages or stack traces mentioned",
    )
    reproduction_steps: str = Field("", description="Steps to reproduce the issue")

    # Reference information
    file_paths: list[str] = Field(
        default_factory=list,
        description="File paths or scripts mentioned in the prompt",
    )
    commit_references: list[str] = Field(
        default_factory=list,
        description="Commit hashes, version numbers, or branch names",
    )
    urls: list[str] = Field(
        default_factory=list,
        description="URLs referenced in the prompt (docs, issues, PRs)",
    )

    # Technical context
    environment: str = Field("", description="Environment or platform details")
    dependencies: list[str] = Field(
        default_factory=list,
        description="Dependencies or versions mentioned",
    )

    # Raw key information
    key_facts: list[str] = Field(
        default_factory=list,
        description="Other key facts extracted from the prompt",
    )

    # Security/CVE build context (for vulnerability reproduction)
    dockerfile: str = Field(
        "",
        description="Dockerfile content for building the vulnerable environment",
    )
    build_script: str = Field(
        "",
        description="Build script (build.sh) content with compilation commands",
    )
    work_dir: str = Field(
        "",
        description="Working directory inside the container",
    )
    poc_command: str = Field(
        "",
        description="Command to execute the PoC/trigger the vulnerability",
    )
    sanitizer: str = Field(
        "",
        description="Sanitizer to use (address, undefined, memory)",
    )
    cve_id: str = Field(
        "",
        description="CVE identifier if this is a vulnerability task",
    )
    repo_url: str = Field(
        "",
        description="Repository URL for cloning",
    )

    # CWE Pattern Inference (inferred from bug report analysis)
    inferred_cwes: list[str] = Field(
        default_factory=list,
        description="Inferred CWE IDs based on bug report analysis (e.g., ['CWE-787', 'CWE-125'])",
    )
    cwe_reasoning: dict[str, str] = Field(
        default_factory=dict,
        description="Reasoning for each inferred CWE (e.g., {'CWE-787': 'Buffer overflow in memcpy...'})",
    )
    cwe_confidence: dict[str, str] = Field(
        default_factory=dict,
        description="Confidence level for each CWE (e.g., {'CWE-787': 'high', 'CWE-125': 'medium'})",
    )
    recommended_sanitizers: list[str] = Field(
        default_factory=list,
        description="Recommended sanitizers based on CWE patterns (e.g., ['-fsanitize=address'])",
    )
    fix_patterns: dict[str, str] = Field(
        default_factory=dict,
        description="Recommended fix patterns per CWE (e.g., {'CWE-787': 'Add bounds check before memcpy'})",
    )

    # Original prompt preserved for full context
    original_prompt: str = Field("", description="The original Boss prompt")


class ContextEntry(BaseModel):
    """Immutable context entry for cross-session knowledge sharing.

    Published by supervisors when workers complete tasks. Other workers
    can query the context dashboard to find relevant entries and learn
    from previous work.

    Can also represent source context extracted from the Boss prompt
    when entry_type is SOURCE.
    """

    model_config = {"frozen": True}

    # Entry ID (assigned when stored in the dashboard)
    entry_id: UUID | None = Field(None, description="Entry UUID in the dashboard")

    # Entry type to distinguish worker context from source context
    entry_type: ContextEntryType = Field(
        default=ContextEntryType.WORKER,
        description="Type of context entry (worker or source)",
    )

    # Key: supervisor-generated descriptive title for the work
    work_title: str = Field(
        ...,
        min_length=1,
        description="Descriptive title for the work (used as key)",
    )

    # Supervisor context (from SubtaskJustification)
    objective: str = Field(..., description="What this subtask aimed to achieve")
    justification: str = Field(..., description="Why this task was assigned")

    # Worker analysis (synthesized from WorkerReport) - optional for SOURCE type
    work_analysis: str = Field(
        "",
        description="Analysis of how the work was executed",
    )

    # Original WorkerReport (preserved for detailed context) - optional for SOURCE type
    worker_report: WorkerReport | None = Field(
        None,
        description="Worker's report if this is a worker context entry",
    )

    # Source context data (only present for SOURCE type entries)
    source_context: SourceContextData | None = Field(
        None,
        description="Extracted source context from Boss prompt (SOURCE type only)",
    )

    # Metadata
    session_id: UUID = Field(description="Root (BOSS) session ID for scoping")
    worker_id: UUID = Field(description="Worker agent that completed this task")
    created_at: datetime

    # Tags for improved relevance matching
    tags: list[str] = Field(
        default_factory=list,
        description="Keywords extracted from task for filtering",
    )
