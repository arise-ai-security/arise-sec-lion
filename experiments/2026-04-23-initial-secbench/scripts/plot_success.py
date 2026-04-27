"""Emit a simple hand-rendered SVG bar chart from `summary.csv`.

Kept dependency-free (no matplotlib) so the seed study runs on a clean
checkout. Real studies should replace this with matplotlib/plotly once
those deps are pinned.
"""

from __future__ import annotations

import csv
import io
import logging
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from experiments.shared.scripts.write_report import write_binary  # noqa: E402


STUDY_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)


def _render_svg(rows: list[dict[str, str]]) -> bytes:
    width, height = 480, 240
    padding = 40
    bar_width = max(24, (width - 2 * padding) // max(1, len(rows)) - 10)
    max_runs = max((int(r.get("runs") or 0) for r in rows), default=1)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Cells overview bar chart">',
        f'<rect width="{width}" height="{height}" fill="#0f172a"/>',
        (
            f'<text x="{width // 2}" y="24" text-anchor="middle" fill="#f8fafc" '
            'font-family="sans-serif" font-size="16" font-weight="600">'
            "SEC-bench matrix — runs per cell</text>"
        ),
    ]
    x = padding
    for row in rows:
        runs = int(row.get("runs") or 0)
        bar_h = int((runs / max_runs) * (height - 2 * padding)) if max_runs else 0
        y = height - padding - bar_h
        parts.append(
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{bar_h}" '
            'fill="#38bdf8"/>'
        )
        label_y = height - padding + 18
        parts.append(
            f'<text x="{x + bar_width // 2}" y="{label_y}" text-anchor="middle" '
            f'fill="#f8fafc" font-family="sans-serif" font-size="12">{row["cell"]}</text>'
        )
        parts.append(
            f'<text x="{x + bar_width // 2}" y="{y - 4}" text-anchor="middle" '
            f'fill="#f8fafc" font-family="sans-serif" font-size="12">{runs}</text>'
        )
        x += bar_width + 10
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rel_study = STUDY_DIR.relative_to(STUDY_DIR.parent.parent).as_posix()
    table_rel = f"{rel_study}/reports/tables/summary.csv"
    table_abs = STUDY_DIR.parent.parent / table_rel
    if not table_abs.is_file():
        raise FileNotFoundError(
            f"expected {table_rel} — run `collect.py` first to produce the summary table"
        )

    rows = list(csv.DictReader(io.StringIO(table_abs.read_text(encoding="utf-8"))))

    output_rel = f"{rel_study}/reports/figures/cells-overview.svg"
    script_rel = f"{rel_study}/scripts/plot_success.py"
    write_binary(
        path=output_rel,
        content=_render_svg(rows),
        script=script_rel,
        inputs=[table_rel],
    )
    logger.info("wrote %s", output_rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
