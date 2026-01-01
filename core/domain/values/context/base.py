"""Base protocol for composable context data.

This module defines the ContextData protocol that all context types must implement
to be usable with the ContextComposer for prompt generation.

The protocol enables:
- Type-safe context composition
- Uniform serialization for Jinja2 templates
- Extensibility for new context types
"""

from abc import abstractmethod
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ContextData(Protocol):
    """Protocol for any data that can be added to prompt context.

    Implementations provide:
    - template_key: Unique key for Jinja2 template access
    - to_template_dict: Serialization for template rendering

    Example implementation:
        class ParentSummary(BaseModel):
            task: str
            result: str | None = None

            @property
            def template_key(self) -> str:
                return "parent_summary"

            def to_template_dict(self) -> dict[str, Any]:
                return self.model_dump()
    """

    @property
    @abstractmethod
    def template_key(self) -> str:
        """Key used in Jinja2 template (e.g., 'parent_summary', 'siblings').

        This key is used to access the data in templates:
            {% if parent_summary %}
            {{ parent_summary.task }}
            {% endif %}
        """
        ...

    @abstractmethod
    def to_template_dict(self) -> dict[str, Any]:
        """Convert to dict for template rendering.

        Returns:
            Dict that will be passed to Jinja2 template under template_key.
        """
        ...
