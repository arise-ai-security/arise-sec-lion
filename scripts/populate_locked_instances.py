#!/usr/bin/env python3
"""Populate base_commit and expected_sanitizer_error in experiments/locked_instances.yaml.

Reads each instance's JSON file, extracts ``base_commit`` and the earliest-occurring
bare sanitizer class from ``sanitizer_report``, and writes the YAML back in-place.

The runner (:mod:`experiments.run_experiment`) reads ``expected_sanitizer_error``
from the YAML -- not from the JSON -- so the bare class must live here.

Idempotent: already-filled fields are left untouched; ``<fill>`` sentinels are replaced.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from experiments.mechanical_evaluator import SANITIZER_ERROR_CLASSES


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCKED = REPO_ROOT / "experiments" / "locked_instances.yaml"


def _extract_bare_sanitizer_class(sanitizer_report: str) -> str | None:
    """Return the earliest-occurring known sanitizer class, or None.

    "Earliest" by character position matches
    :func:`experiments.mechanical_evaluator.classify_sanitizer_output` so the
    locked value aligns with what the mechanical evaluator produces at run time.
    """
    text_lower = sanitizer_report.lower()
    matches: list[tuple[int, str]] = []
    for cls in SANITIZER_ERROR_CLASSES:
        idx = text_lower.find(cls.lower())
        if idx >= 0:
            matches.append((idx, cls))
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


def _needs_fill(value: Any) -> bool:
    """Return True if ``value`` is missing or still a ``<...>`` placeholder."""
    if not value:
        return True
    text = str(value)
    return text.startswith("<") and text.endswith(">")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not LOCKED.exists():
        logger.error("locked_instances.yaml not found at %s", LOCKED)
        return 1

    data = yaml.safe_load(LOCKED.read_text(encoding="utf-8"))
    filled = 0
    skipped: list[str] = []

    for inst in data["instances"]:
        instance_id = inst["instance_id"]
        json_path = REPO_ROOT / inst["json_path"]
        if not json_path.exists():
            logger.warning("Missing CVE JSON for %s: %s", instance_id, json_path)
            skipped.append(instance_id)
            continue

        cve_data = json.loads(json_path.read_text(encoding="utf-8"))

        if _needs_fill(inst.get("base_commit")):
            base_commit = cve_data.get("base_commit", "")
            if not base_commit:
                logger.warning("%s: JSON has no base_commit", instance_id)
            inst["base_commit"] = base_commit
            filled += 1

        if _needs_fill(inst.get("expected_sanitizer_error")):
            sanitizer_report = cve_data.get("sanitizer_report", "")
            bare = _extract_bare_sanitizer_class(sanitizer_report)
            if bare is None:
                logger.warning(
                    "%s: could not extract bare sanitizer class from sanitizer_report (len=%d)",
                    instance_id,
                    len(sanitizer_report),
                )
                skipped.append(instance_id)
                continue
            inst["expected_sanitizer_error"] = bare
            filled += 1

    if filled == 0:
        logger.info("Nothing to fill; %s left untouched (preserves comments)", LOCKED)
    else:
        LOCKED.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        logger.info("Wrote %s (filled %d field(s))", LOCKED, filled)
    if skipped:
        logger.warning("Skipped/partial instances: %s", skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
