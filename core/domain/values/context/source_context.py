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

    @property
    def template_key(self) -> str:
        """Key for template variable injection (ContextData protocol)."""
        return "source_context"

    def to_template_dict(self) -> dict[str, Any]:
        """Convert to dict for Jinja2 template rendering (ContextData protocol)."""
        return self.model_dump()

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
        )
