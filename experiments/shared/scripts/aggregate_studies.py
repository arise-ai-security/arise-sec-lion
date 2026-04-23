"""Cross-study aggregation (§8 optional tool).

The actual analysis is intentionally thin — this script is a structural
placeholder so meta-analysis scripts can be added later without re-inventing
manifest discovery. It walks every `experiments/<study>/manifest.yaml`,
loads the enrolled runs, and emits a JSON summary on stdout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

import yaml

from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.load_runs import load_runs


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


logger = logging.getLogger(__name__)


def _study_manifests() -> Iterable[Path]:
    experiments = get_repo_root() / "experiments"
    if not experiments.is_dir():
        return
    for study_dir in sorted(experiments.iterdir()):
        if not study_dir.is_dir():
            continue
        manifest = study_dir / "manifest.yaml"
        if manifest.is_file():
            yield manifest


def aggregate() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for manifest_path in _study_manifests():
        study = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        study_id = study.get("study_id") or manifest_path.parent.name
        runs_index = study.get("runs") or []
        enrolled_ids = {str(row.get("run_id")) for row in runs_index}
        matching = [r for r in load_runs(study_id=study_id) if r.get("run_id") in enrolled_ids]

        summaries.append(
            {
                "study_id": study_id,
                "enrolled": len(runs_index),
                "resolved_manifests": len(matching),
                "cells": sorted({row.get("cell") for row in runs_index if row.get("cell")}),
            }
        )
    return summaries


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="aggregate_studies",
        description="Summarize every study's enrollment index as JSON on stdout.",
    )
    parser.parse_args(argv)
    json.dump(aggregate(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
