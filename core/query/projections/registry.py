"""Component registry for pipeline extensibility via decorators."""

from collections.abc import Callable
from typing import ClassVar, TypeVar


T = TypeVar("T")


class RegistryError(Exception):
    """Registry lookup or registration failed."""


class ProjectionRegistry:
    """Central registry for filters, projections, formatters, and sinks."""

    _filters: ClassVar[dict[str, type]] = {}
    _projections: ClassVar[dict[str, type]] = {}
    _formatters: ClassVar[dict[str, type]] = {}
    _sinks: ClassVar[dict[str, type]] = {}

    @classmethod
    def register_filter(cls, name: str) -> Callable[[type[T]], type[T]]:
        return cls._make_registrar(cls._filters, "filter", name)

    @classmethod
    def register_projection(cls, name: str) -> Callable[[type[T]], type[T]]:
        return cls._make_registrar(cls._projections, "projection", name)

    @classmethod
    def register_formatter(cls, name: str) -> Callable[[type[T]], type[T]]:
        return cls._make_registrar(cls._formatters, "formatter", name)

    @classmethod
    def register_sink(cls, name: str) -> Callable[[type[T]], type[T]]:
        return cls._make_registrar(cls._sinks, "sink", name)

    @classmethod
    def get_filter(cls, name: str) -> type:
        """Look up registered filter by name."""
        return cls._get_from(cls._filters, "filter", name)

    @classmethod
    def get_projection(cls, name: str) -> type:
        """Look up registered projection by name."""
        return cls._get_from(cls._projections, "projection", name)

    @classmethod
    def get_formatter(cls, name: str) -> type:
        """Look up registered formatter by name."""
        return cls._get_from(cls._formatters, "formatter", name)

    @classmethod
    def get_sink(cls, name: str) -> type:
        """Look up registered sink by name."""
        return cls._get_from(cls._sinks, "sink", name)

    @classmethod
    def list_filters(cls) -> list[str]:
        return sorted(cls._filters.keys())

    @classmethod
    def list_formatters(cls) -> list[str]:
        return sorted(cls._formatters.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registrations (for testing)."""
        cls._filters.clear()
        cls._projections.clear()
        cls._formatters.clear()
        cls._sinks.clear()

    @classmethod
    def _make_registrar(
        cls, registry: dict[str, type], component_type: str, name: str
    ) -> Callable[[type[T]], type[T]]:
        def decorator(cls_to_register: type[T]) -> type[T]:
            if name in registry:
                existing = registry[name].__name__
                raise RegistryError(
                    f"{component_type.capitalize()} '{name}' already registered to {existing}"
                )
            registry[name] = cls_to_register
            return cls_to_register

        return decorator

    @classmethod
    def _get_from(cls, registry: dict[str, type], component_type: str, name: str) -> type:
        if name not in registry:
            available = ", ".join(sorted(registry.keys())) or "(none)"
            raise RegistryError(f"Unknown {component_type}: '{name}'. Available: {available}")
        return registry[name]


def register_filter(name: str) -> Callable[[type[T]], type[T]]:
    return ProjectionRegistry.register_filter(name)


def register_projection(name: str) -> Callable[[type[T]], type[T]]:
    return ProjectionRegistry.register_projection(name)


def register_formatter(name: str) -> Callable[[type[T]], type[T]]:
    return ProjectionRegistry.register_formatter(name)


def register_sink(name: str) -> Callable[[type[T]], type[T]]:
    return ProjectionRegistry.register_sink(name)
