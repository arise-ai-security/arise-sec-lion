from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConstraintFailure:
    """Value object for when LLM cannot satisfy execution constraints.

    Returned when LLM responds with constraints_unsatisfiable instead of
    a valid subtask list. This allows graceful failure rather than
    spawning doomed workers.
    """

    reason: str
    minimum_subtasks: int | None = None
    minimum_depth: int | None = None

    @classmethod
    def matches(cls, data: Any) -> bool:
        """Check if data represents a constraint failure response."""
        return isinstance(data, dict) and data.get("status") == "constraints_unsatisfiable"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'ConstraintFailure':
        """Create from LLM response dict."""
        minimum_required = data.get("minimum_required", {})
        return cls(
            reason=data.get("reason", "Unknown reason"),
            minimum_subtasks=minimum_required.get("subtasks"),
            minimum_depth=minimum_required.get("depth_levels"),
        )

    def format_message(self) -> str:
        """Format failure as human-readable message."""
        msg = f"Constraints unsatisfiable: {self.reason}"
        if self.minimum_subtasks is not None:
            msg += f" (needs at least {self.minimum_subtasks} subtasks)"
        if self.minimum_depth is not None:
            msg += f" (needs {self.minimum_depth} more depth levels)"
        return msg
