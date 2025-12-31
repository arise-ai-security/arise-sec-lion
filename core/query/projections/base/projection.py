"""Base class for event projections."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from core.domain.events.events import DomainEvent


class Projection(ABC):
    """Transform domain events into structured output."""

    @abstractmethod
    def project(self, events: Iterable[DomainEvent]) -> Any:
        """Transform events into output (type depends on implementation)."""
        ...
