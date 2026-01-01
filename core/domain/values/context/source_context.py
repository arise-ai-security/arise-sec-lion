"""Source context for worker execution (Design Choice 6).

Provides extracted key information from the user's original task
to all workers in the hierarchy.
"""

from typing import Any

from pydantic import BaseModel, Field


class SourceContext(BaseModel):
    """Source context extracted from user prompt for workers (Design Choice 6).

    Implements ContextData protocol for use with ContextComposer.
    Provides key information extracted by BOSS to inform worker execution.

    Design Choice 7 additions: CWE inference and fix patterns.
    """

    model_config = {"frozen": True}

    # Extraction summary
    extraction_summary: str = ""

    # Reference types detected
    key_references: tuple[str, ...] = ()
    has_bug_report: bool = False
    has_error_details: bool = False
    has_file_references: bool = False
    has_code_snippets: bool = False

    # Structured entities extracted
    extracted_entities: dict[str, Any] = Field(default_factory=dict)

    # Design Choice 7: CWE Context Passing
    inferred_cwes: tuple[str, ...] = ()  # e.g., ("CWE-787", "CWE-125")
    cwe_reasoning: dict[str, str] = Field(default_factory=dict)  # Per-CWE reasoning
    recommended_sanitizers: tuple[str, ...] = ()  # e.g., ("AddressSanitizer", "UBSan")
    fix_patterns: dict[str, str] = Field(default_factory=dict)  # Per-CWE fix patterns

    @property
    def template_key(self) -> str:
        """Key for template variable injection (ContextData protocol)."""
        return "source_context"

    def to_template_dict(self) -> dict[str, Any]:
        """Convert to dict for Jinja2 template rendering (ContextData protocol)."""
        return self.model_dump()

    @property
    def has_cwe_context(self) -> bool:
        """Check if CWE context is available."""
        return len(self.inferred_cwes) > 0

    @classmethod
    def from_extraction_event(
        cls,
        extraction_summary: str,
        key_references: tuple[str, ...] = (),
        has_bug_report: bool = False,
        has_error_details: bool = False,
        has_file_references: bool = False,
        has_code_snippets: bool = False,
        extracted_entities: dict[str, Any] | None = None,
        inferred_cwes: tuple[str, ...] = (),
        cwe_reasoning: dict[str, str] | None = None,
        recommended_sanitizers: tuple[str, ...] = (),
        fix_patterns: dict[str, str] | None = None,
    ) -> "SourceContext":
        """Create SourceContext from extraction event data."""
        return cls(
            extraction_summary=extraction_summary,
            key_references=key_references,
            has_bug_report=has_bug_report,
            has_error_details=has_error_details,
            has_file_references=has_file_references,
            has_code_snippets=has_code_snippets,
            extracted_entities=extracted_entities or {},
            inferred_cwes=inferred_cwes,
            cwe_reasoning=cwe_reasoning or {},
            recommended_sanitizers=recommended_sanitizers,
            fix_patterns=fix_patterns or {},
        )
