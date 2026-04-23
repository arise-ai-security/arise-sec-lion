"""Run persistence: last-run pointer and per-run manifest writer."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from presentation.persistence.invocation_hash import compute_invocation_sha256


if TYPE_CHECKING:
    from config import Settings


logger = logging.getLogger(__name__)


class _CostView(Protocol):
    """Shape of the cost block the manifest writer needs from a summary.

    Kept local to presentation so run_persistence does not import from core.
    The attributes are treated as read-only here; widening them to read/write
    would add needless invariance constraints on any conforming class.
    """

    @property
    def prompt_tokens(self) -> int: ...
    @property
    def completion_tokens(self) -> int: ...
    @property
    def cost_by_model(self) -> dict[str, float]: ...


class _SummaryView(Protocol):
    """Shape of a projection summary the manifest writer consumes.

    Only the cost attribute (or its absence) is read; everything else on the
    real ``ProjectionSummary`` is irrelevant here.
    """

    @property
    def cost(self) -> _CostView | None: ...


RunStatus = Literal["success", "timeout", "failed"]


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


def _current_git_sha() -> str | None:
    """Return the current HEAD sha, or None when git isn't usable."""
    git_bin = shutil.which("git")
    if git_bin is None:
        logger.warning("git not found on PATH; omitting git_sha from run manifest")
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git_bin, "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        logger.warning("git rev-parse HEAD failed; omitting git_sha from run manifest")
        return None
    return result.stdout.strip() or None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON atomically: tmp file in same dir + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


class RunPersistence:
    """Handles per-run persistence: last-run pointer and manifest writing.

    Single Responsibility: All file I/O for run-level metadata lives here so
    `bootstrap/bootstrap.py::_run_task` can call a single method after the
    CLI returns without touching core or plugin layers.
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

    async def write_run_manifest(
        self,
        run_id: UUID,
        *,
        settings: Settings,
        task: str,
        domain_context_path: Path | None,
        exit_status: RunStatus,
        wall_started_at: datetime,
        wall_ended_at: datetime,
        summary: _SummaryView | None,
        kind: str = "ours",
    ) -> Path:
        """Write `runs/<run_id>/run_manifest.json` with runtime-level fields.

        Returns the written path. Safe to call even when the run directory
        does not exist yet; it's created as needed.
        """
        run_dir = self._output_dir / str(run_id)
        manifest_path = run_dir / "run_manifest.json"

        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "kind": kind,
            "started_at": _to_iso(wall_started_at),
            "ended_at": _to_iso(wall_ended_at),
            "exit_status": exit_status,
            "git_sha": _current_git_sha(),
            "invocation_sha256": compute_invocation_sha256(
                settings=settings,
                task=task,
                domain_context_path=domain_context_path,
            ),
            "models": {
                "boss": settings.boss.model,
                "manager": settings.manager.model,
                "worker": settings.worker.model,
            },
            "summary_available": summary is not None,
            "deliverables": _probe_deliverables(run_dir),
        }
        # Only emit tokens / costs_by_model when a real summary was produced.
        # Zeros-as-fallback would be indistinguishable from a genuinely empty
        # run; the summary_available flag is the durable signal instead.
        if summary is not None:
            payload["tokens"] = _tokens_block(summary)
            payload["costs_by_model"] = _costs_by_model(summary)

        _atomic_write_json(manifest_path, payload)
        return manifest_path


def _to_iso(moment: datetime) -> str:
    """Return an ISO8601 string in UTC with a trailing Z."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _tokens_block(summary: _SummaryView) -> dict[str, int]:
    cost = summary.cost
    if cost is None:
        return {"prompt": 0, "completion": 0}
    return {
        "prompt": int(cost.prompt_tokens),
        "completion": int(cost.completion_tokens),
    }


def _costs_by_model(summary: _SummaryView) -> dict[str, float]:
    cost = summary.cost
    if cost is None or not cost.cost_by_model:
        return {}
    return {str(model): float(amount) for model, amount in cost.cost_by_model.items()}


def _probe_deliverables(run_dir: Path) -> dict[str, bool]:
    """Report top-level files inside ``run_dir/testcase`` as {filename: True}.

    Probing the filesystem keeps this layer domain-agnostic: whatever the
    runtime happened to drop into ``testcase/`` is what gets recorded, with
    no hardcoded SEC-bench filenames baked into presentation. A missing
    directory yields an empty dict.
    """
    testcase_dir = run_dir / "testcase"
    if not testcase_dir.is_dir():
        return {}
    return {child.name: True for child in testcase_dir.iterdir() if child.is_file()}
