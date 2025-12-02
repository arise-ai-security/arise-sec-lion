"""Abstract base classes and protocols for the projection system.

This package contains the abstract interfaces that define the contracts
for all projection pipeline components.
"""

from core.application.projections.base.filter import EventFilter
from core.application.projections.base.formatter import Formatter
from core.application.projections.base.projection import Projection


__all__ = [
    "EventFilter",
    "Formatter",
    "Projection",
]
