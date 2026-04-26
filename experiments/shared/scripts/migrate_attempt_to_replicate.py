"""One-shot migration: copy `attempt` → `replicate` in every run_manifest.json.

PR 1 of the experiments rearchitecture renames the field. ``attempt`` is
kept as a one-cycle alias so both names coexist; PR 6 (Task 7) drops
``attempt`` entirely. This script is idempotent: running it twice is a
no-op the second time, and a manifest that already carries both fields
is left untouched.

Usage::

    python -m experiments.shared.scripts.migrate_attempt_to_replicate
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from experiments.shared.scripts.load_runs import default_pool_roots, iter_run_manifests


if TYPE_CHECKING:
    from collections.abc import Iterable


logger = logging.getLogger(__name__)


def _migrate_one(manifest_path: Path) -> bool:
    """Copy `attempt` to `replicate` if missing. Returns True if file changed."""
    try:
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("skipping unreadable manifest %s: %s", manifest_path, exc)
        return False

    if not isinstance(record, dict):
        logger.warning("skipping non-object manifest: %s", manifest_path)
        return False

    if "replicate" in record:
        return False
    if "attempt" not in record:
        return False

    record["replicate"] = record["attempt"]

    tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(manifest_path)
    return True


def migrate(pool_roots: Iterable[Path] | None = None) -> int:
    """Walk every pool root and migrate each manifest. Returns the change count."""
    changed = 0
    scanned = 0
    for manifest_path in iter_run_manifests(pool_roots=pool_roots):
        scanned += 1
        if _migrate_one(manifest_path):
            changed += 1
            logger.info("migrated %s", manifest_path)
    logger.info("scanned %d manifests, migrated %d", scanned, changed)
    return changed


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_attempt_to_replicate",
        description="Copy `attempt` to `replicate` in every run_manifest.json (idempotent).",
    )
    parser.add_argument(
        "--pool-root",
        action="append",
        type=Path,
        dest="pool_roots",
        help="Override pool roots (repeatable); defaults to runs/ + settings.output.directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_arg_parser().parse_args(argv)
    pool_roots = args.pool_roots if args.pool_roots else default_pool_roots()
    migrate(pool_roots=pool_roots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
