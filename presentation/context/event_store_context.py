"""Async context manager for event store lifecycle."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Settings
    from core.ports.event_store_port import EventStorePort


class EventStoreContext:
    """Async context manager for event store lifecycle.

    Ensures proper connection/disconnection handling.
    Follows RAII pattern for resource management.

    Usage:
        async with EventStoreContext.from_settings(settings) as event_store:
            events = await event_store.get_events(agent_id)
    """

    def __init__(self, event_store: EventStorePort) -> None:
        self._event_store = event_store
        self._connected = False

    @classmethod
    def from_connection_string(cls, connection_string: str) -> EventStoreContext:
        """Create context from PostgreSQL connection string."""
        from infrastructure.adapters.postgres_event_store import PostgresEventStore

        event_store = PostgresEventStore(connection_string)
        return cls(event_store)

    @classmethod
    def from_settings(cls, settings: Settings) -> EventStoreContext:
        """Create context from Settings object."""
        return cls.from_connection_string(settings.database.connection_string)

    @classmethod
    def from_config_path(cls, config_path: Path | str | None = None) -> EventStoreContext:
        """Create context from config file path.

        Args:
            config_path: Optional path to YAML config. If None, loads default.
        """
        from config import Settings

        if config_path:
            settings = Settings.from_yaml(config_path)
        else:
            settings = Settings.load()

        return cls.from_settings(settings)

    async def __aenter__(self) -> EventStorePort:
        """Connect to event store and return it."""
        await self._event_store.connect()
        self._connected = True
        return self._event_store

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Disconnect from event store."""
        if self._connected:
            await self._event_store.disconnect()
            self._connected = False
