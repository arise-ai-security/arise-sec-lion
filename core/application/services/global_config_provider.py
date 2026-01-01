"""Global configuration provider for system-wide context.

This module provides a service that supplies global configuration
to all agents in the hierarchy. Global config is injected into
every prompt via the ContextComposer.

Example usage:
    provider = GlobalConfigProvider()
    provider.set("current_date", "2025-12-31")
    provider.set("execution_id", "abc-123")

    # In pipeline step:
    composer.add(provider.to_global_config())
"""

from datetime import date
from typing import Any

from core.domain.values.context import GlobalConfig


class GlobalConfigProvider:
    """Provides global configuration context to all agents.

    This service manages system-wide configuration that should be
    available to every agent in the execution hierarchy. It follows
    the Provider pattern for flexible configuration injection.

    Thread Safety:
        Not thread-safe. Create one instance per execution run.

    Example:
        provider = GlobalConfigProvider()
        provider.set("current_date", "2025-12-31")
        provider.set("target", {"host": "192.168.1.100"})

        config = provider.to_global_config()
        composer.add(config)
    """

    __slots__ = ("_data",)

    def __init__(self) -> None:
        """Initialize with empty configuration."""
        self._data: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> "GlobalConfigProvider":
        """Set a configuration value.

        Args:
            key: Configuration key (e.g., "current_date", "target").
            value: Configuration value (can be any JSON-serializable type).

        Returns:
            Self for method chaining.
        """
        self._data[key] = value
        return self

    def set_current_date(self, date_value: date | str | None = None) -> "GlobalConfigProvider":
        """Set the current date in the global config.

        Convenience method for adding the current date to global context.
        If no date is provided, uses today's date.

        Args:
            date_value: Date to set. Can be:
                - date object: Formatted as YYYY-MM-DD
                - str: Used as-is
                - None: Uses today's date

        Returns:
            Self for method chaining.
        """
        if date_value is None:
            date_value = date.today()

        if isinstance(date_value, date):
            date_str = date_value.isoformat()
        else:
            date_str = date_value

        return self.set("current_date", date_str)

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value.

        Args:
            key: Configuration key.
            default: Value to return if key not found.

        Returns:
            Configuration value or default.
        """
        return self._data.get(key, default)

    def update(self, data: dict[str, Any]) -> "GlobalConfigProvider":
        """Update multiple configuration values.

        Args:
            data: Dictionary of key-value pairs to add.

        Returns:
            Self for method chaining.
        """
        self._data.update(data)
        return self

    def to_global_config(self) -> GlobalConfig:
        """Convert to GlobalConfig context data.

        Creates an immutable GlobalConfig from the current configuration
        that can be added to a ContextComposer.

        Returns:
            GlobalConfig instance for use with ContextComposer.
        """
        return GlobalConfig(data=self._data.copy())

    def reset(self) -> "GlobalConfigProvider":
        """Clear all configuration.

        Returns:
            Self for method chaining.
        """
        self._data.clear()
        return self

    def __bool__(self) -> bool:
        """True if any configuration is set."""
        return bool(self._data)

    def __len__(self) -> int:
        """Return number of configuration keys."""
        return len(self._data)

    def __repr__(self) -> str:
        """String representation showing keys."""
        return f"GlobalConfigProvider({list(self._data.keys())})"
