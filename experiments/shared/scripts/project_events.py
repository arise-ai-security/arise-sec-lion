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
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from config import Settings
from core.query.projections import ProjectionPipelineBuilder
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
    with output_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json())
            handle.write("\n")

    logger.info("wrote %d events to %s", len(events), output_path)
    return len(events)


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
