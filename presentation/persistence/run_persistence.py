"""Run persistence for tracking last run state (SRP compliant)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class LastRunInfo:
    """Immutable record of last run information."""

    boss_id: UUID
    task: str
    started_at: datetime
    status: str

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "boss_id": str(self.boss_id),
            "task": self.task,
            "started_at": self.started_at.isoformat(),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LastRunInfo:
        """Deserialize from dictionary."""
        return cls(
            boss_id=UUID(data["boss_id"]),
            task=data["task"],
            started_at=datetime.fromisoformat(data["started_at"]),
            status=data["status"],
        )


class RunPersistence:
    """Handles last run persistence operations.

    Single Responsibility: Only manages reading/writing last run state.
    """

    FILENAME = ".last_run.json"

    def __init__(self, output_dir: Path | str) -> None:
        self._output_dir = Path(output_dir)

    @property
    def _path(self) -> Path:
        return self._output_dir / self.FILENAME

    def get_last_run(self) -> LastRunInfo | None:
        """Read last run info from disk."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
            return LastRunInfo.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def save_last_run(
        self,
        boss_id: UUID,
        task: str,
        status: str,
    ) -> None:
        """Save last run info to disk."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        info = LastRunInfo(
            boss_id=boss_id,
            task=task,
            started_at=datetime.now(),
            status=status,
        )
        self._path.write_text(json.dumps(info.to_dict(), indent=2))

    def get_last_run_id(self) -> UUID | None:
        """Get boss_id from last run, if available."""
        info = self.get_last_run()
        return info.boss_id if info else None
