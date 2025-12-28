"""Worker base - re-exports from worker.shared for backward compatibility.

DEPRECATED: Import from infrastructure.adapters.worker.shared instead.
"""

from infrastructure.adapters.worker.shared import validate_task_context

__all__ = ["validate_task_context"]
