"""Snapshot the effective merged config alongside a run's manifest.

When a run completes, write an ``effective_config.yaml`` file inside
``runs/<run_id>/`` containing the fully-resolved Settings (overlay extends
+ overrides applied + env-var injection). This is the per-run provenance
artifact future researchers read to know exactly what configuration ran.

The dump redacts ``database.password`` when it is sourced from the optional
``POSTGRES_PASSWORD`` environment variable. Every other Settings field is
either YAML-defined or a non-sensitive runtime default, so it is safe to
record verbatim.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml


if TYPE_CHECKING:
    from uuid import UUID

    from config.settings import Settings


logger = logging.getLogger(__name__)


SNAPSHOT_FILENAME = "effective_config.yaml"


def snapshot_effective_config(*, run_id: UUID, settings: Settings, run_dir: Path) -> Path:
    """Write the resolved Settings as ``effective_config.yaml`` in ``run_dir``.

    Returns the absolute path of the snapshot. The write is atomic
    (temp file in the same directory followed by ``replace``) so partial
    failures never leave a half-written file behind.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / SNAPSHOT_FILENAME

    payload = _redact(settings.model_dump(mode="json"))
    payload["run_id"] = str(run_id)

    serialized = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(run_dir))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(serialized)
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Replace known-sensitive fields with a placeholder before serialization.

    ``database.password`` may be sourced from ``POSTGRES_PASSWORD`` and must
    not end up in a per-run artifact that researchers may share or archive.
    """
    database = payload.get("database")
    if isinstance(database, dict) and "password" in database:
        database = dict(database)
        database["password"] = "***"  # noqa: S105 - redaction sentinel, not a credential
        payload = dict(payload)
        payload["database"] = database
    return payload
