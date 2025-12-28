"""OpenHands Adapter - re-exports from worker package for backward compatibility.

DEPRECATED: Import from infrastructure.adapters.worker instead.
"""

from infrastructure.adapters.worker.openhands_adapter import OpenHandsAdapter

__all__ = ["OpenHandsAdapter"]
