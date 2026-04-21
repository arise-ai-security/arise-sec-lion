"""Per-instance progress-stage classification: A1/A2 (v1) vs B1/B2 (v2).

Instead of relying on the mechanical evaluator (which is still broken
upstream -- every run shows builder=pass but exploiter+fixer=fail because
of the cross-phase container churn), this script reads the actual
artifacts each run left on disk and classifies how far through the
secbench 4-phase pipeline (Builder → Exploiter → Fixer → Reporter) each
agent made it.

Artifact locations
------------------
* A cells (flat CLI): the agent writes to ``<run_dir>/workspace/``. All
  files observed live directly under that directory.
* B cells (tree): each spawned worker mounts its own workspace at
  ``<run_dir>/<agent_uuid>/testcase/``. Artifacts are the UNION across
  all agent UUIDs in the run directory (any worker producing the file
  counts for the run, since the tree's Reporter phase reads every
  sibling's testcase/).

Stage ladder
------------
0. ``none``         -- no artifacts on disk (or only a cwd skeleton).
1. ``build``        -- only build-phase artifacts (base_commit_hash,
                       repo_changes.diff, packages.txt).
2. ``poc``          -- PoC artifact exists (``poc``, ``poc.*``, or
                       ``repro.sh``).
3. ``patch``        -- a patch file exists (``model_patch.diff`` or
                       ``fix.patch`` or ``cve-*.patch``).
4. ``report``       -- synthesis report exists (``security_report.md``).

Deepest stage reached is reported. Individual artifact flags are also
emitted per-run so the reader can see exactly what the agent produced.

Output
------
Four markdown tables (A1, A2, B1, B2) + a per-cell summary.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
V1_RUNS = ROOT / "dataset" / "runs"
V2_RUNS = ROOT / "dataset-v2-20260420" / "runs"

PRIMARY_CVES: tuple[str, ...] = (
    "njs.cve-2022-32414",
    "njs.cve-2022-38890",
    "faad2.cve-2021-32272",
    "faad2.cve-2018-20196",
    "mruby.cve-2022-0240",
    "gpac.cve-2022-1795",
    "gpac.cve-2021-40575",
    "openjpeg.cve-2016-7445",
    "imagemagick.cve-2019-13309",
    "exiv2.cve-2017-14859",
)

# Patch filenames we accept as "fixer phase produced output". Ordered from
# most-canonical to most-permissive. Any match = has_patch=True.
_PATCH_NAMES_EXACT = ("model_patch.diff", "fix.patch", "repo_changes.diff")
_PATCH_NAME_RE = re.compile(r"^(?:cve-[0-9\-]+\.patch|.*\.patch)$", re.IGNORECASE)

# PoC file patterns. "poc", "poc.js", "poc.bin", "poc_1.c", etc.
_POC_NAME_RE = re.compile(r"^poc(?:[._].+)?$", re.IGNORECASE)

# Reproduction shell script used by secbench's repro phase.
_REPRO_NAMES = ("repro.sh",)

# Build-phase artefacts -- any one of these indicates the builder did
# something beyond a no-op.
_BUILD_NAMES = ("base_commit_hash", "packages.txt")
_BUILD_DIFF_NAMES = ("repo_changes.diff",)

# Reporter synthesis. ``security_report.md`` is the canonical secbench name;
# accept a small set of close variants so a mis-spelled deliverable still
# counts as "reporter produced output".
_REPORT_NAMES = (
    "security_report.md",
    "security-report.md",
    "report.md",
    "findings.md",
)


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """Boolean flags summarising artifacts present in a run directory."""

    has_base_commit: bool
    has_build_customization: bool  # repo_changes.diff or packages.txt
    has_poc: bool
    has_repro: bool
    has_patch: bool
    has_report: bool

    @property
    def has_any_build(self) -> bool:
        return self.has_base_commit or self.has_build_customization

    @property
    def has_exploiter_output(self) -> bool:
        return self.has_poc or self.has_repro

    def deepest_stage(self) -> str:
        if self.has_report:
            return "4_report"
        if self.has_patch:
            return "3_patch"
        if self.has_exploiter_output:
            return "2_poc"
        if self.has_any_build:
            return "1_build"
        return "0_none"


def _iter_files_safely(root: Path):
    """Yield every readable file under ``root``; swallow OS / permission errors.

    ``rglob`` walks the tree and can raise ``PermissionError`` / ``OSError``
    on unreadable subdirectories (observed on some njs build trees with
    unicode test file paths). A single unreadable child must not abort the
    whole scan, so we iterate manually and skip entries we can't stat.
    """
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return
    for entry in entries:
        try:
            is_dir = entry.is_dir()
            is_file = entry.is_file()
        except (OSError, PermissionError):
            continue
        if is_file:
            yield entry
        elif is_dir:
            yield from _iter_files_safely(entry)


def _scan_dir(root: Path) -> RunArtifacts:
    """Collect artifact flags by scanning every file under ``root``."""
    if not root.exists():
        return RunArtifacts(False, False, False, False, False, False)
    has_base_commit = False
    has_build_customization = False
    has_poc = False
    has_repro = False
    has_patch = False
    has_report = False
    for p in _iter_files_safely(root):
        name = p.name
        if name == "base_commit_hash":
            has_base_commit = True
        if name in _BUILD_NAMES:
            has_base_commit = has_base_commit or name == "base_commit_hash"
        if name in _BUILD_DIFF_NAMES or name == "packages.txt":
            has_build_customization = True
        if name in _REPRO_NAMES:
            has_repro = True
        if _POC_NAME_RE.match(name):
            has_poc = True
        if name in _PATCH_NAMES_EXACT or _PATCH_NAME_RE.match(name):
            # Treat repo_changes.diff as build customization, NOT fixer patch.
            if name == "repo_changes.diff":
                has_build_customization = True
            else:
                has_patch = True
        if name in _REPORT_NAMES:
            has_report = True
    return RunArtifacts(
        has_base_commit=has_base_commit,
        has_build_customization=has_build_customization,
        has_poc=has_poc,
        has_repro=has_repro,
        has_patch=has_patch,
        has_report=has_report,
    )


def scan_a_run(cve_id: str, cell: str) -> RunArtifacts:
    """A-cell scan: single workspace/ directly under run_dir."""
    run_dir = V1_RUNS / cve_id / cell / "0"
    return _scan_dir(run_dir / "workspace")


def scan_b_run(cve_id: str, cell: str) -> RunArtifacts:
    """B-cell scan: each agent_uuid mounts its own testcase/; union across them.

    Also check the top-level workspace/ in case a flat deliverable landed
    there. Agent-UUID dirs are any immediate sub-directory whose name is a
    UUID (hex-36-with-dashes) -- the tree adapter creates them per spawn.
    """
    run_dir = V2_RUNS / cve_id / cell / "0"
    if not run_dir.exists():
        return RunArtifacts(False, False, False, False, False, False)

    uuid_re = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        re.IGNORECASE,
    )

    # Scan every agent_uuid/testcase/ + the top-level workspace/.
    arts: list[RunArtifacts] = [_scan_dir(run_dir / "workspace")]
    try:
        children = list(run_dir.iterdir())
    except (OSError, PermissionError):
        children = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except (OSError, PermissionError):
            continue
        if uuid_re.match(child.name):
            arts.append(_scan_dir(child / "testcase"))

    return RunArtifacts(
        has_base_commit=any(a.has_base_commit for a in arts),
        has_build_customization=any(a.has_build_customization for a in arts),
        has_poc=any(a.has_poc for a in arts),
        has_repro=any(a.has_repro for a in arts),
        has_patch=any(a.has_patch for a in arts),
        has_report=any(a.has_report for a in arts),
    )


def _short_cve(cve: str) -> str:
    project, _, rest = cve.partition(".")
    return f"{project}:{rest.removeprefix('cve-')}"


STAGES = ["0_none", "1_build", "2_poc", "3_patch", "4_report"]


def build_rows() -> list[dict]:
    rows = []
    for cve in PRIMARY_CVES:
        for cell in ("A1", "A2"):
            art = scan_a_run(cve, cell)
            rows.append(_row(cve, cell, art))
        for cell in ("B1", "B2"):
            art = scan_b_run(cve, cell)
            rows.append(_row(cve, cell, art))
    return rows


def _row(cve: str, cell: str, art: RunArtifacts) -> dict:
    return {
        "cve": cve,
        "cve_short": _short_cve(cve),
        "cell": cell,
        "stage": art.deepest_stage(),
        "base_commit": art.has_base_commit,
        "build_extra": art.has_build_customization,
        "poc": art.has_poc,
        "repro": art.has_repro,
        "patch": art.has_patch,
        "report": art.has_report,
    }


def render_per_cell_table(rows: list[dict], cells: tuple[str, str]) -> str:
    cves = sorted({r["cve_short"] for r in rows})
    lines = [
        "| cell / CVE | " + " | ".join(cves) + " |",
        "|" + "---|" * (len(cves) + 1),
    ]
    by_cve = {}
    for r in rows:
        by_cve.setdefault(r["cell"], {})[r["cve_short"]] = r
    for cell in cells:
        cell_rows = by_cve.get(cell, {})
        stage_cells = [cell_rows[c]["stage"] if c in cell_rows else "—" for c in cves]
        lines.append(f"| {cell}.stage | " + " | ".join(stage_cells) + " |")
        for flag_label, flag_key in [
            ("build", "base_commit"),
            ("build+", "build_extra"),
            ("poc", "poc"),
            ("repro", "repro"),
            ("patch", "patch"),
            ("report", "report"),
        ]:
            flag_cells = [
                "✓" if c in cell_rows and cell_rows[c][flag_key] else " "
                for c in cves
            ]
            lines.append(f"| {cell}.{flag_label} | " + " | ".join(flag_cells) + " |")
    return "\n".join(lines)


def render_stage_summary(rows: list[dict]) -> str:
    """How many CVEs reached each stage, per cell."""
    lines = [
        "| stage | A1 | A2 | B1 | B2 |",
        "|---|---|---|---|---|",
    ]
    counts = {cell: Counter(r["stage"] for r in rows if r["cell"] == cell) for cell in ("A1", "A2", "B1", "B2")}
    for stage in STAGES:
        row = [stage]
        for cell in ("A1", "A2", "B1", "B2"):
            row.append(str(counts[cell].get(stage, 0)))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("|---|---|---|---|---|")
    lines.append(
        "| reached ≥ build | "
        + " | ".join(
            str(sum(counts[cell].get(s, 0) for s in STAGES[1:])) for cell in ("A1", "A2", "B1", "B2")
        )
        + " |"
    )
    lines.append(
        "| reached ≥ poc | "
        + " | ".join(
            str(sum(counts[cell].get(s, 0) for s in STAGES[2:])) for cell in ("A1", "A2", "B1", "B2")
        )
        + " |"
    )
    lines.append(
        "| reached ≥ patch | "
        + " | ".join(
            str(sum(counts[cell].get(s, 0) for s in STAGES[3:])) for cell in ("A1", "A2", "B1", "B2")
        )
        + " |"
    )
    lines.append(
        "| reached report | "
        + " | ".join(str(counts[cell].get("4_report", 0)) for cell in ("A1", "A2", "B1", "B2"))
        + " |"
    )
    return "\n".join(lines)


def main() -> None:
    rows = build_rows()

    print("## Per-instance progress: A cells (flat CLI)\n")
    print(render_per_cell_table(rows, ("A1", "A2")))
    print()
    print("## Per-instance progress: B cells (tree)\n")
    print(render_per_cell_table(rows, ("B1", "B2")))
    print()
    print("## Stage reach summary (counts of 10 CVEs per cell)\n")
    print(render_stage_summary(rows))


if __name__ == "__main__":
    main()
