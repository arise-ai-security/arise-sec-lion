#!/usr/bin/env python3
"""Export completed N1 results for handoff."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = ROOT_DIR / "experiments/n1-secbench-full/export_data.py"


def main(argv: list[str] | None = None) -> int:
    """Export the local N1 runs and event database."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    uv = shutil.which("uv")
    if uv is None:
        logger.error("export failed: execute experiments/n1-secbench-full/setup.py first")
        return 1
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        subprocess.run(
            [uv, "run", "python", str(EXPORT_SCRIPT), *arguments],
            cwd=ROOT_DIR,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error("export failed with status %d", exc.returncode)
        return exc.returncode
    except KeyboardInterrupt:
        logger.error("export interrupted; rerun the same command")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
