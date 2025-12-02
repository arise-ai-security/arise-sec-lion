"""Component registry for plugin-like extensibility.

This module provides a registry system that allows components (filters, projections,
formatters, sinks) to be registered via decorators and looked up by name at runtime.

Usage:
    @register_filter("errors_only")
    class ErrorOnlyFilter:
        ...

    # Later:
    filter_cls = ProjectionRegistry.get_filter("errors_only")
    filter_instance = filter_cls()
"""

from collections.abc import Callable
from typing import ClassVar, TypeVar


T = TypeVar("T")


class RegistryError(Exception):
    """Raised when registry operations fail."""


class ProjectionRegistry:
    """Central registry for projection pipeline components.

    Stores mappings from string names to component classes, enabling
    dynamic lookup and instantiation. Uses class-level dictionaries
    that are shared across all usages.

    Supported component types:
        - filters: Event filtering strategies
        - projections: Event-to-output transformations
        - formatters: Output format converters
        - sinks: Output destinations

    Thread Safety:
        Registration is typically done at import time, so concurrent
        access during runtime is read-only and safe.
    """

    _filters: ClassVar[dict[str, type]] = {}
    _projections: ClassVar[dict[str, type]] = {}
    _formatters: ClassVar[dict[str, type]] = {}
    _sinks: ClassVar[dict[str, type]] = {}

    @classmethod
    def register_filter(cls, name: str) -> Callable[[type[T]], type[T]]:
        """Decorator to register a filter class.

        Args:
            name: Unique identifier for this filter.

        Returns:
            Decorator function that registers the class.

        Raises:
            RegistryError: If name is already registered.

        Example:
            @ProjectionRegistry.register_filter("all")
            class IncludeAllFilter:
                ...
        """
        return cls._make_registrar(cls._filters, "filter", name)

    @classmethod
    def register_projection(cls, name: str) -> Callable[[type[T]], type[T]]:
        """Decorator to register a projection class.

        Args:
            name: Unique identifier for this projection.

        Returns:
            Decorator function that registers the class.

        Raises:
            RegistryError: If name is already registered.
        """
        return cls._make_registrar(cls._projections, "projection", name)

    @classmethod
    def register_formatter(cls, name: str) -> Callable[[type[T]], type[T]]:
        """Decorator to register a formatter class.

        Args:
            name: Unique identifier for this formatter.

        Returns:
            Decorator function that registers the class.

        Raises:
            RegistryError: If name is already registered.
        """
        return cls._make_registrar(cls._formatters, "formatter", name)

    @classmethod
    def register_sink(cls, name: str) -> Callable[[type[T]], type[T]]:
        """Decorator to register a sink class.

        Args:
            name: Unique identifier for this sink.

        Returns:
            Decorator function that registers the class.

        Raises:
            RegistryError: If name is already registered.
        """
        return cls._make_registrar(cls._sinks, "sink", name)

    @classmethod
    def get_filter(cls, name: str) -> type:
        """Look up a registered filter by name.

        Args:
            name: The registered name of the filter.

        Returns:
            The filter class.

        Raises:
            RegistryError: If name is not registered.
        """
        return cls._get_from(cls._filters, "filter", name)

    @classmethod
    def get_projection(cls, name: str) -> type:
        """Look up a registered projection by name.

        Args:
            name: The registered name of the projection.

        Returns:
            The projection class.

        Raises:
            RegistryError: If name is not registered.
        """
        return cls._get_from(cls._projections, "projection", name)

    @classmethod
    def get_formatter(cls, name: str) -> type:
        """Look up a registered formatter by name.

        Args:
            name: The registered name of the formatter.

        Returns:
            The formatter class.

        Raises:
            RegistryError: If name is not registered.
        """
        return cls._get_from(cls._formatters, "formatter", name)

    @classmethod
    def get_sink(cls, name: str) -> type:
        """Look up a registered sink by name.

        Args:
            name: The registered name of the sink.

        Returns:
            The sink class.

        Raises:
            RegistryError: If name is not registered.
        """
        return cls._get_from(cls._sinks, "sink", name)

    @classmethod
    def list_filters(cls) -> list[str]:
        """List all registered filter names.

        Returns:
            Sorted list of filter names.
        """
        return sorted(cls._filters.keys())

    @classmethod
    def list_projections(cls) -> list[str]:
        """List all registered projection names.

        Returns:
            Sorted list of projection names.
        """
        return sorted(cls._projections.keys())

    @classmethod
    def list_formatters(cls) -> list[str]:
        """List all registered formatter names.

        Returns:
            Sorted list of formatter names.
        """
        return sorted(cls._formatters.keys())

    @classmethod
    def list_sinks(cls) -> list[str]:
        """List all registered sink names.

        Returns:
            Sorted list of sink names.
        """
        return sorted(cls._sinks.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registrations.

        Primarily intended for testing to reset state between tests.
        """
        cls._filters.clear()
        cls._projections.clear()
        cls._formatters.clear()
        cls._sinks.clear()

    @classmethod
    def _make_registrar(
        cls, registry: dict[str, type], component_type: str, name: str
    ) -> Callable[[type[T]], type[T]]:
        """Create a decorator that registers a class.

        Args:
            registry: The dictionary to register in.
            component_type: Human-readable type name for error messages.
            name: The name to register under.

        Returns:
            Decorator function.

        Raises:
            RegistryError: If name is already registered.
        """

        def decorator(cls_to_register: type[T]) -> type[T]:
            if name in registry:
                existing = registry[name].__name__
                raise RegistryError(
                    f"{component_type.capitalize()} '{name}' is already registered "
                    f"to {existing}. Choose a different name."
                )
            registry[name] = cls_to_register
            return cls_to_register

        return decorator

    @classmethod
    def _get_from(cls, registry: dict[str, type], component_type: str, name: str) -> type:
        """Look up a component from a registry.

        Args:
            registry: The dictionary to look up in.
            component_type: Human-readable type name for error messages.
            name: The name to look up.

        Returns:
            The registered class.

        Raises:
            RegistryError: If name is not found.
        """
        if name not in registry:
            available = ", ".join(sorted(registry.keys())) or "(none)"
            raise RegistryError(f"Unknown {component_type}: '{name}'. Available: {available}")
        return registry[name]


# Convenience decorator aliases at module level
def register_filter(name: str) -> Callable[[type[T]], type[T]]:
    """Decorator to register a filter class.

    This is a convenience alias for ProjectionRegistry.register_filter().

    Args:
        name: Unique identifier for this filter.

    Returns:
        Decorator function that registers the class.
    """
    return ProjectionRegistry.register_filter(name)


def register_projection(name: str) -> Callable[[type[T]], type[T]]:
    """Decorator to register a projection class.

    This is a convenience alias for ProjectionRegistry.register_projection().

    Args:
        name: Unique identifier for this projection.

    Returns:
        Decorator function that registers the class.
    """
    return ProjectionRegistry.register_projection(name)


def register_formatter(name: str) -> Callable[[type[T]], type[T]]:
    """Decorator to register a formatter class.

    This is a convenience alias for ProjectionRegistry.register_formatter().

    Args:
        name: Unique identifier for this formatter.

    Returns:
        Decorator function that registers the class.
    """
    return ProjectionRegistry.register_formatter(name)


def register_sink(name: str) -> Callable[[type[T]], type[T]]:
    """Decorator to register a sink class.

    This is a convenience alias for ProjectionRegistry.register_sink().

    Args:
        name: Unique identifier for this sink.

    Returns:
        Decorator function that registers the class.
    """
    return ProjectionRegistry.register_sink(name)
