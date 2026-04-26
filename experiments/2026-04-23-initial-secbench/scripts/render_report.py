"""Render `reports/report.md` from the Jinja template, summary CSV, and lockfile.

Uses `write_md` so the output carries YAML frontmatter with input and
output sha256s. The validator will reject any post-hoc edit to the body.
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

import jinja2  # noqa: E402
import yaml  # noqa: E402

from experiments.shared.scripts.collect import (  # noqa: E402
    ENROLLMENT_LOCK_FILENAME,
    load_enrollment_lock,
)
from experiments.shared.scripts.write_report import write_md  # noqa: E402


STUDY_DIR = Path(__file__).resolve().parent.parent
STUDY_ID = STUDY_DIR.name
logger = logging.getLogger(__name__)


def _render(rows: list[dict[str, str]], manifest: dict, enrollment: list[dict]) -> str:
    template_dir = STUDY_DIR / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        autoescape=False,  # noqa: S701 - markdown output, not HTML
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.md.j2")

    cells = [
        {"id": name, "harness": entry.get("harness", ""), "config": entry.get("config", "")}
        for name, entry in (manifest.get("cells") or {}).items()
    ]
    table_rows = [
        {
            "cell": row["cell"],
            "count": row["runs"],
            "deliverables": row["deliverables_present"],
        }
        for row in rows
    ]
    legacy_count = sum(
        1 for r in enrollment if str(r.get("legacy_migration", False)).lower() == "true"
    )
    return template.render(
        hypothesis=manifest.get("hypothesis", ""),
        cells=cells,
        total_runs=len(enrollment),
        legacy_runs=legacy_count,
        live_runs=len(enrollment) - legacy_count,
        figure_path="figures/cells-overview.svg",
        table_rows=table_rows,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rel_study = STUDY_DIR.relative_to(STUDY_DIR.parent.parent).as_posix()

    manifest_rel = f"{rel_study}/manifest.yaml"
    table_rel = f"{rel_study}/reports/tables/summary.csv"
    figure_rel = f"{rel_study}/reports/figures/cells-overview.svg"
    enrollment_rel = f"{rel_study}/reports/{ENROLLMENT_LOCK_FILENAME}"
    template_rel = f"{rel_study}/templates/report.md.j2"
    output_rel = f"{rel_study}/reports/report.md"
    script_rel = f"{rel_study}/scripts/render_report.py"

    manifest_abs = STUDY_DIR.parent.parent / manifest_rel
    table_abs = STUDY_DIR.parent.parent / table_rel
    for required in (manifest_abs, table_abs):
        if not required.is_file():
            raise FileNotFoundError(
                f"missing dependency {required.name}; run `collect.py` and `plot_success.py` first"
            )

    manifest = yaml.safe_load(manifest_abs.read_text(encoding="utf-8"))
    rows = list(csv.DictReader(io.StringIO(table_abs.read_text(encoding="utf-8"))))
    enrollment_doc = load_enrollment_lock(STUDY_ID)
    enrollment = enrollment_doc.get("enrollment") or []

    write_md(
        path=output_rel,
        content=_render(rows, manifest, enrollment),
        script=script_rel,
        template=template_rel,
        inputs=[manifest_rel, table_rel, figure_rel, enrollment_rel],
    )
    logger.info("wrote %s", output_rel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
