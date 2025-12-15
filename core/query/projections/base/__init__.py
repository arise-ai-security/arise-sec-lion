"""Abstract base classes and protocols for the projection system.

This package contains the abstract interfaces that define the contracts
for all projection pipeline components.
"""

from core.query.projections.base.filter import EventFilter
from core.query.projections.base.formatter import Formatter
from core.query.projections.base.projection import Projection


__all__ = [
    "EventFilter",
    "Formatter",
    "Projection",
]
