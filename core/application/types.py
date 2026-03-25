"""Shared application-layer type aliases."""

from collections.abc import Callable
from typing import Any

from core.domain.events.events import DomainEvent


type ProgressCallback = Callable[[DomainEvent, Any], None]
