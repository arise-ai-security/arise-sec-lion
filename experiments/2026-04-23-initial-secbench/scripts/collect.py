"""Collect run manifests for this study into a CSV table.

Reads enrolled runs from `manifest.yaml`, loads each `run_manifest.json`
from the runs/ pool (legacy + live), computes per-cell summary stats, and
writes `reports/tables/summary.csv` with scripts-first sidecar metadata.
"""

from __future__ import annotations

import io
import logging
import sys
from csv import DictWriter
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from experiments.shared.scripts.load_runs import load_runs  # noqa: E402
from experiments.shared.scripts.write_report import write_binary  # noqa: E402


STUDY_DIR = Path(__file__).resolve().parent.parent
STUDY_ID = STUDY_DIR.name


logger = logging.getLogger(__name__)


def _load_cells() -> list[str]:
    manifest = yaml.safe_load((STUDY_DIR / "manifest.yaml").read_text(encoding="utf-8"))
    return sorted((manifest or {}).get("cells", {}).keys())


def _summarize(cells: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cell in cells:
        runs = load_runs(study_id=STUDY_ID, cells=[cell])
        deliverables = 0
        for run in runs:
            probe = run.get("deliverables") or {}
            deliverables += sum(1 for v in probe.values() if v)
        rows.append(
            {
                "cell": cell,
                "runs": len(runs),
                "deliverables_present": deliverables,
            }
        )
    return rows


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = DictWriter(
        buffer,
        fieldnames=["cell", "runs", "deliverables_present"],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cells = _load_cells()
    rows = _summarize(cells)

    rel_study = STUDY_DIR.relative_to(STUDY_DIR.parent.parent).as_posix()
    output_rel = f"{rel_study}/reports/tables/summary.csv"
    script_rel = f"{rel_study}/scripts/collect.py"
    manifest_rel = f"{rel_study}/manifest.yaml"

    write_binary(
        path=output_rel,
        content=_csv_bytes(rows),
        script=script_rel,
        inputs=[manifest_rel],
    )
    logger.info("wrote %s (%d cells)", output_rel, len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
