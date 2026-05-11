"""Project a run's hierarchy of events from Postgres to `runs/<run_id>/events.jsonl`.

Reads through the existing CQRS projection machinery so the event-sourcing
ground truth stays authoritative. Invoked automatically by the harness right
after `main.py run` returns; can also be run manually for historical runs.

Usage::

    python -m experiments.shared.scripts.project_events --run-id <uuid>
    python -m experiments.shared.scripts.project_events \\
        --run-id <uuid> --output runs/<uuid>/events.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from config import Settings
from core.query.projections import ProjectionPipelineBuilder
from infrastructure.adapters import sinks as _sinks  # noqa: F401 — registers @register_sink classes
from infrastructure.adapters.postgres_event_store import PostgresEventStore


if TYPE_CHECKING:
    from core.domain.events.events import DomainEvent


logger = logging.getLogger(__name__)


def _resolve_settings(
    settings: Settings | None, config_path: Path | None
) -> Settings:
    """Honor an explicit config path before falling back to the default loader.

    Study runs pin specific configs via `--config`; the harness must pass
    that same config here so the projection queries the right Postgres
    instance and the events.jsonl matches the run that just executed.
    """
    if settings is not None:
        return settings
    if config_path is not None:
        return Settings.from_yaml(config_path)
    return Settings.load()


async def project_events_to_jsonl(
    *,
    run_id: UUID,
    output_path: Path,
    settings: Settings | None = None,
    config_path: Path | None = None,
) -> int:
    """Collect the run's event hierarchy and write it as JSON Lines.

    Returns the number of events written. Raises if the run has no events.
    """
    resolved_settings = _resolve_settings(settings, config_path)
    store = PostgresEventStore(resolved_settings.database.connection_string)
    await store.connect()
    try:
        pipeline = ProjectionPipelineBuilder(store).build()
        events: list[DomainEvent] = await pipeline.execute_events(run_id)
    finally:
        await store.disconnect()

    if not events:
        raise ValueError(f"no events found for run_id={run_id}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_events_jsonl(output_path, events)
    logger.info("wrote %d events to %s", len(events), output_path)
    return len(events)


def _atomic_write_events_jsonl(output_path: Path, events: list[DomainEvent]) -> None:
    """Write events to a tmp file with fsync, then atomically replace target.

    Crash-safety contract: a reader sees either the full new file or the
    previous file (or no file). A killed harness mid-write cannot leave a
    truncated `events.jsonl` that the reader silently skips lines from
    (audit N-1). Mirrors the tmp+fsync+replace pattern in
    ``presentation/persistence/run_persistence.py:_atomic_write_json``.

    Each line is the event's `model_dump(mode="json")` payload extended with
    an explicit ``event_type`` discriminator (audit N-5). The base
    ``DomainEvent`` schema declares no class-name field, so without the
    discriminator the metrics reader has to infer the type from a brittle
    set of field-tuple heuristics that miss 25 of 32 subclasses.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=output_path.name + ".",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for event in events:
                # Build the payload then overwrite the discriminator last so a
                # future DomainEvent subclass that happens to declare its own
                # ``event_type`` field cannot shadow the class name we need
                # for downstream dispatch.
                payload = event.model_dump(mode="json")
                payload["event_type"] = type(event).__name__
                handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    # Best-effort directory fsync so the rename is durable across a kernel
    # panic. Outside the outer try/except because the replace already
    # succeeded: the file IS on disk, so a fsync failure must not stamp
    # projection_status=failed on a healthy run.
    try:
        dir_fd = os.open(str(output_path.parent), os.O_DIRECTORY)
    except (OSError, AttributeError):
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            logger.warning(
                "directory fsync failed for %s: %s; file written but rename "
                "may not survive a kernel panic",
                output_path.parent,
                exc,
            )
    finally:
        os.close(dir_fd)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project_events",
        description=(
            "Project a run's event hierarchy from Postgres to "
            "`runs/<run_id>/events.jsonl` for offline analysis."
        ),
    )
    parser.add_argument("--run-id", type=UUID, required=True, help="Root BOSS agent UUID")
    parser.add_argument(
        "--output",
        type=Path,
        help="Override output path (default: <settings.output.directory>/<run-id>/events.jsonl)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Pinned config path (matches `python main.py -c <config> run`)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    settings = _resolve_settings(settings=None, config_path=args.config)
    default_output = (
        Path(settings.output.directory) / str(args.run_id) / "events.jsonl"
    )
    output_path = args.output or default_output
    asyncio.run(
        project_events_to_jsonl(
            run_id=args.run_id,
            output_path=output_path,
            settings=settings,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
