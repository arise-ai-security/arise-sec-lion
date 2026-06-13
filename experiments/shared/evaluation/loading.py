"""Run loading: the only module that touches the Postgres event store.

``load_run`` fetches the full agent hierarchy for a run via the canonical
``PostgresEventStore`` + ``get_hierarchy_events_grouped`` path, reads the on-disk
``run_manifest.json``, and returns a :class:`RunData` that every (pure) metric
function consumes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID

from config.settings import ApiSettings
from core.domain.events.events import ChildSpawned
from experiments.shared.evaluation.models import RunData


logger = logging.getLogger(__name__)

# experiments/shared/evaluation/loading.py → repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]
RUNS_DIR = REPO_ROOT / "runs"

TOPOLOGY_BEF = "bef"
TOPOLOGY_LINEAR = "linear"


def _coerce_run_id(run_id: str | UUID) -> UUID:
    return run_id if isinstance(run_id, UUID) else UUID(str(run_id))


def _read_manifest(run_dir: Path) -> dict[str, object]:
    manifest_path = run_dir / "run_manifest.json"
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        logger.warning("No run_manifest.json under %s", run_dir)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", manifest_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _flatten_grouped(grouped: dict[UUID, list]) -> list:
    """Flatten {aggregate_id: [events]} into one globally time-ordered list."""
    flat = [event for events in grouped.values() for event in events]
    flat.sort(key=lambda e: (e.occurred_at, str(e.aggregate_id), e.sequence_number))
    return flat


async def load_run(run_id: str | UUID, *, runs_dir: Path = RUNS_DIR) -> RunData:
    """Load all events (from Postgres) and artifacts metadata for a run.

    Args:
        run_id: The run/root (BOSS) aggregate id; also the ``runs/<run_id>/`` dir.
        runs_dir: Override the runs directory (for tests).

    Returns:
        A :class:`RunData` bundling the flat event stream, run directory, and
        parsed manifest.
    """
    root_id = _coerce_run_id(run_id)
    settings = ApiSettings.load()

    # Import here so importing this package never requires asyncpg unless a load
    # actually happens (keeps pure-function imports/tests DB-free).
    from infrastructure.adapters.postgres_event_store import PostgresEventStore

    store = PostgresEventStore(settings.database.connection_string)
    await store.connect()
    try:
        grouped = await store.get_hierarchy_events_grouped(root_id)
    finally:
        await store.disconnect()

    run_dir = runs_dir / str(root_id)
    return RunData(
        run_id=root_id,
        events=_flatten_grouped(grouped),
        run_dir=run_dir,
        manifest=_read_manifest(run_dir),
    )


def detect_topology(run_id: str | UUID, *, runs_dir: Path = RUNS_DIR) -> str:
    """Classify a run as ``"bef"`` (B*) or ``"linear"`` (N*) from its manifest.

    Reads ``run_manifest.json`` only (no DB). Raises ``ValueError`` if neither
    ``cell`` nor ``study_id`` can decide it.
    """
    root_id = _coerce_run_id(run_id)
    manifest = _read_manifest(runs_dir / str(root_id))
    cell = str(manifest.get("cell", "")).strip().upper()
    study = str(manifest.get("study_id", "")).strip().lower()
    if cell.startswith("N") or study.startswith("n"):
        return TOPOLOGY_LINEAR
    if cell.startswith("B") or study.startswith("b"):
        return TOPOLOGY_BEF
    raise ValueError(
        f"Cannot determine topology for run {root_id} from manifest "
        f"(cell={cell!r}, study_id={study!r}); use topology_of(run_data) instead."
    )


def topology_of(run_data: RunData) -> str:
    """Structural topology of a loaded run: hierarchical (BEF) vs linear.

    A run is hierarchical if it spawned any child agent; otherwise linear.
    """
    has_children = any(isinstance(e, ChildSpawned) for e in run_data.events)
    distinct_aggregates = {e.aggregate_id for e in run_data.events}
    if has_children or len(distinct_aggregates) > 1:
        return TOPOLOGY_BEF
    return TOPOLOGY_LINEAR
