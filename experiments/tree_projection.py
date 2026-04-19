"""TODO(experiment-runner): Project tree event-store events into events.jsonl + meta.json.

Stub. The tree arm (cells B1, B2) writes its events to the Postgres event
store during execution. The dataset's normalized format requires an
``events.jsonl`` alongside each run directory; producing that from the event
store is not implemented for v1.0.

The experiment runner invokes ``python -m experiments.tree_projection
--run-dir <plan.run_dir>`` after each tree dispatch. Until this module is
implemented:

* the command logs a clear warning,
* exits 0 so :func:`subprocess.run(..., check=False)` does not treat the
  missing projection as a runner error,
* no ``events.jsonl`` is produced -- the audit + mechanical evaluation
  downstream will naturally be skipped for those runs.

Tracking: Task 16 (integration smoke) will surface this as a concrete gap.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


logger = logging.getLogger(__name__)


def main() -> int:
    """CLI stub: log a clear TODO message and exit 0 so the caller keeps running."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Tree run projection (STUB).")
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    logger.warning(
        "tree_projection is a STUB. No events.jsonl will be produced for "
        "run-dir=%s. Implement experiments/tree_projection.py before running "
        "the tree arm at scale.",
        args.run_dir,
    )
    # Exit 0: the calling subprocess uses check=False; a non-zero exit would
    # still not stop the runner, but a clean 0 keeps logs unambiguous.
    return 0


if __name__ == "__main__":
    sys.exit(main())
