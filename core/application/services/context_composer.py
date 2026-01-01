"""Composable context builder for prompt generation.

This module provides the ContextComposer class, a fluent builder for
composing prompt context from multiple ContextData instances.

The composer enables:
- Programmatic context selection (not config-driven)
- Type-safe composition via ContextData protocol
- Method chaining for clean API
- Conditional and optional data inclusion

Usage:
    context = (
        ContextComposer()
        .add(ParentSummary(task="...", result="..."))
        .add(SiblingResults(siblings=[...]))
        .add_if(has_fixer, AncestorData(label="fixer", ...))
        .add_optional(maybe_decisions)
    )

    template_vars = context.build()
"""

from typing import Any

from core.domain.values.context.base import ContextData


class ContextComposer:
    """Fluent builder for composing prompt context.

    Collects ContextData instances and merges them into a single
    template context dict. Supports method chaining.

    Thread Safety:
        Not thread-safe. Create a new instance per request.

    Example:
        context = (
            ContextComposer()
            .add(ParentSummary(task="...", result="..."))
            .add(SiblingResults(siblings=[...]))
            .add(AncestorData(label="fixer", ...))
        )

        template_vars = context.build()
        # {"parent_summary": {...}, "sibling_results": {...}, "ancestor_fixer": {...}}
    """

    __slots__ = ("_items",)

    def __init__(self) -> None:
        """Initialize empty composer."""
        self._items: list[ContextData] = []

    def add(self, data: ContextData) -> "ContextComposer":
        """Add context data to the composition.

        Args:
            data: Any object implementing ContextData protocol.

        Returns:
            Self for method chaining.

        Raises:
            TypeError: If data doesn't implement ContextData protocol.
        """
        if not isinstance(data, ContextData):
            raise TypeError(
                f"Expected ContextData, got {type(data).__name__}. "
                "Ensure the object has template_key property and to_template_dict method."
            )
        self._items.append(data)
        return self

    def add_if(self, condition: bool, data: ContextData) -> "ContextComposer":
        """Conditionally add context data.

        Args:
            condition: Only add if True.
            data: Context data to add.

        Returns:
            Self for method chaining.

        Example:
            context.add_if(agent.parent_id is not None, parent_summary)
        """
        if condition:
            return self.add(data)
        return self

    def add_optional(self, data: ContextData | None) -> "ContextComposer":
        """Add context data if not None.

        Args:
            data: Context data to add, or None to skip.

        Returns:
            Self for method chaining.

        Example:
            context.add_optional(maybe_get_fixer_context())
        """
        if data is not None:
            return self.add(data)
        return self

    def add_all(self, items: list[ContextData]) -> "ContextComposer":
        """Add multiple context data items.

        Args:
            items: List of ContextData to add.

        Returns:
            Self for method chaining.
        """
        for item in items:
            self.add(item)
        return self

    def merge(self, other: "ContextComposer") -> "ContextComposer":
        """Merge another composer's items into this one.

        Args:
            other: Another ContextComposer to merge from.

        Returns:
            Self for method chaining.

        Example:
            base_context = ContextComposer().add(parent_summary)
            extended = ContextComposer().add(siblings).merge(base_context)
        """
        self._items.extend(other._items)
        return self

    def build(self) -> dict[str, Any]:
        """Build final template context dict.

        Merges all added items by their template_key.
        Later additions override earlier ones with same key.

        Returns:
            Dict suitable for Jinja2 template rendering.
            Keys are the template_key values from each ContextData.
            Values are the dicts from to_template_dict().

        Example:
            {"parent_summary": {...}, "sibling_results": {...}}
        """
        result: dict[str, Any] = {}
        for item in self._items:
            result[item.template_key] = item.to_template_dict()
        return result

    def has(self, template_key: str) -> bool:
        """Check if a specific context type was added.

        Args:
            template_key: The template key to check for.

        Returns:
            True if any added item has this template_key.

        Example:
            if context.has("parent_summary"):
                # Include parent context in prompt
        """
        return any(item.template_key == template_key for item in self._items)

    def has_any(self, *template_keys: str) -> bool:
        """Check if any of the specified context types were added.

        Args:
            template_keys: Template keys to check for.

        Returns:
            True if any added item has one of the specified template_keys.
        """
        key_set = set(template_keys)
        return any(item.template_key in key_set for item in self._items)

    def keys(self) -> list[str]:
        """Get list of all template keys in the composition.

        Returns:
            List of template_key values from all added items.
        """
        return [item.template_key for item in self._items]

    def clear(self) -> "ContextComposer":
        """Clear all added items.

        Returns:
            Self for method chaining.
        """
        self._items.clear()
        return self

    def copy(self) -> "ContextComposer":
        """Create a shallow copy of this composer.

        Returns:
            New ContextComposer with same items.
        """
        new_composer = ContextComposer()
        new_composer._items = list(self._items)
        return new_composer

    def __len__(self) -> int:
        """Return number of context items."""
        return len(self._items)

    def __bool__(self) -> bool:
        """True if any context items were added."""
        return len(self._items) > 0

    def __repr__(self) -> str:
        """String representation showing keys."""
        keys = self.keys()
        return f"ContextComposer({keys})"
