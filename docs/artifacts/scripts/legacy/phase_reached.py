"""Phase-reached analysis for the T2 matrix (A1, A2, B1, B2).

For each of the 10 primary CVE runs per cell, detect whether the agent
*reached* each of the four SEC-bench pipeline phases regardless of
whether the deliverable passed the mechanical evaluator:

    1. Build     -- builder produced compile/build evidence
    2. PoC       -- exploiter authored a proof-of-concept or ran a
                    reproduction against the built binary
    3. Patch     -- fixer wrote a candidate patch (file or in-container
                    source edit in the `src`/upstream tree)
    4. Report    -- reporter wrote a structured summary markdown

Detection combines two signals per run:

* **Workspace-file scan.** For A cells the agent's workspace is
  ``<run_dir>/workspace/``. For B cells each spawned worker mounts its
  own ``<run_dir>/<agent_uuid>/testcase/``; artefacts are the union
  across every UUID sub-directory plus the top-level ``workspace/``.
  Pre-staged scaffold files (the crash input copied in by ``secb`` at
  container build time, the ``base_commit_hash`` seed, etc.) are
  explicitly excluded from PoC detection so we only credit files the
  agent actually authored.

* **Event-log scan.** ``events.jsonl`` captures every tool call. Flat
  A-cell runs make all their edits inside the Docker container's
  ``/tmp/`` or ``/src/`` tree which is not persisted back to
  ``<run_dir>/workspace/``; the events are the only trace that a
  build / patch / report ever happened. For B cells events reveal
  phase tags on ``child_spawned`` (``[Builder]``, ``[Exploiter]``,
  ``[Fixer]``, ``[Reporter]``) and ``work_completed.data.result``
  summary text.

A phase is marked "reached" if **either** source produces evidence. The
heuristic is deliberately *lenient* (the question is production, not
evaluation) but we guard against obvious false positives:

* Source-file edits that look like build-system scratch (``exv_conf.h``,
  CMake cache files, auto-generated files) do NOT count as patching.
* Only writes under the upstream source tree (``/src/``, or a CVE's
  cloned project directory) count as patching.
* PoC detection requires either a workspace file whose name matches a
  PoC pattern AND whose name does NOT match the pre-staged crash-input
  pattern, OR an agent Write/Edit of a poc/repro file, OR a Bash
  invocation of the built binary against a crash input.

The script emits:

1. A 4x4 summary table (rows = A1/A2/B1/B2, cols = Build/PoC/Patch/
   Report, values = "N / 10").
2. A per-CVE breakdown per cell.
"""

from __future__ import annotations

import json
import re
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

CELLS: tuple[str, ...] = ("A1", "A2", "B1", "B2")
PHASES: tuple[str, ...] = ("build", "poc", "patch", "report")

# --------------------------------------------------------------------- #
# Workspace-file heuristics
# --------------------------------------------------------------------- #

# Pre-staged crash-input files ``secb`` drops into /testcase before any
# agent runs. If ONLY these exist the PoC phase has NOT been reached.
# Each CVE's secb_sh references a specific crash file; discovered
# empirically from plugins/security/tests/fixtures/<cve>.json.
_PRESTAGED_NAMES: dict[str, frozenset[str]] = {
    "njs.cve-2022-32414": frozenset({"poc.js"}),
    "njs.cve-2022-38890": frozenset({"poc.js"}),
    "faad2.cve-2021-32272": frozenset({
        "heap-overflow-stszin-mp4read-355",
        "heap-overflow-stszin-mp4read-355.zip",
    }),
    "faad2.cve-2018-20196": frozenset({
        "013-stack-buffer-overflow-sbr_hfadj_1287",
    }),
    "mruby.cve-2022-0240": frozenset({"poc"}),
    "gpac.cve-2022-1795": frozenset({"POC1"}),
    "gpac.cve-2021-40575": frozenset({
        "mp4box-seg-npd-mpgviddmx_process643",
        "mp4box-seg-npd-mpgviddmx_process643.zip",
    }),
    "openjpeg.cve-2016-7445": frozenset({
        "openjpeg-nullptr-github-issue-842.ppm",
    }),
    "imagemagick.cve-2019-13309": frozenset(set()),
    "exiv2.cve-2017-14859": frozenset({"005-invalid-mem.tiff"}),
}

# Files always pre-staged by secb scaffolding regardless of CVE. These
# are never a signal of agent activity.
_UNIVERSAL_PRESTAGED: frozenset[str] = frozenset({
    "base_commit_hash",
})

# PoC name patterns the agent creates.
_POC_NAME_RE = re.compile(
    r"^(?:poc(?:[._].+)?|repro(?:\.sh|duce\.sh)?|crash(?:[_\-\.].+)?|asan_.*\.(?:log|txt))$",
    re.IGNORECASE,
)

# Patch filenames. `repo_changes.diff` is auto-captured by secb during
# build customisation, so it is NOT a fixer-phase signal.
_PATCH_NAME_RE = re.compile(
    r"^(?:model_patch\.diff|fix\.patch|cve-[0-9\-]+\.patch|patch\.diff|.*_patch\.diff)$",
    re.IGNORECASE,
)

# Builder deliverables: recording of the base commit, build-script
# customisation, dependency manifest. The `base_commit_hash` file alone
# is not sufficient (it is pre-staged), but `packages.txt` or
# `build_output.log` or a `repo_changes.diff` that is NON-EMPTY
# indicates the builder did something.
_BUILD_FILE_NAMES = {
    "packages.txt",
    "build.sh",
    "build_output.log",
    "build_verify.txt",
    "build_verification.txt",
    "asan_symbols.txt",          # ASan-instrumentation confirmation
    "asan_verification.txt",
}

# Reporter synthesis files.
_REPORT_NAME_RE = re.compile(
    r"^(?:security[_-]?report|report|findings|summary|root_cause|"
    r"fix_summary|crash_summary|builder_report)\.md$",
    re.IGNORECASE,
)

# UUID directory (B-cell per-agent workspace).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_prestaged(name: str, cve_id: str) -> bool:
    if name in _UNIVERSAL_PRESTAGED:
        return True
    return name in _PRESTAGED_NAMES.get(cve_id, frozenset())


def _iter_files_safely(root: Path):
    """Yield every readable file under *root*; swallow OS errors."""
    if not root.exists():
        return
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return
    for entry in entries:
        try:
            if entry.is_file():
                yield entry
            elif entry.is_dir():
                yield from _iter_files_safely(entry)
        except (OSError, PermissionError):
            continue


@dataclass(frozen=True, slots=True)
class WorkspaceFlags:
    build: bool
    poc: bool
    patch: bool
    report: bool


def _scan_workspace(root: Path, cve_id: str) -> WorkspaceFlags:
    build = poc = patch = report = False
    if not root.exists():
        return WorkspaceFlags(False, False, False, False)

    for p in _iter_files_safely(root):
        name = p.name

        # Reporter
        if _REPORT_NAME_RE.match(name):
            report = True

        # Patch
        if _PATCH_NAME_RE.match(name):
            patch = True

        # Builder. `repo_changes.diff` is explicitly NOT a build
        # signal -- secb auto-captures whatever is diffed in the source
        # tree, which may be source patches the agent made. The real
        # builder deliverables are packages.txt / build_output.log /
        # explicit build_verify / asan_* verification files.
        if name in _BUILD_FILE_NAMES:
            build = True

        # PoC: skip files that secb pre-stages into /testcase before
        # the agent runs. Only agent-authored PoC / repro artefacts
        # count.
        if _is_prestaged(name, cve_id):
            continue
        if _POC_NAME_RE.match(name):
            poc = True

    return WorkspaceFlags(build, poc, patch, report)


def scan_a_workspace(cve_id: str, cell: str) -> WorkspaceFlags:
    return _scan_workspace(V1_RUNS / cve_id / cell / "0" / "workspace", cve_id)


def scan_b_workspace(cve_id: str, cell: str) -> WorkspaceFlags:
    run_dir = V2_RUNS / cve_id / cell / "0"
    if not run_dir.exists():
        return WorkspaceFlags(False, False, False, False)
    flags = [_scan_workspace(run_dir / "workspace", cve_id)]
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
        if _UUID_RE.match(child.name):
            flags.append(_scan_workspace(child / "testcase", cve_id))
    return WorkspaceFlags(
        build=any(f.build for f in flags),
        poc=any(f.poc for f in flags),
        patch=any(f.patch for f in flags),
        report=any(f.report for f in flags),
    )


# --------------------------------------------------------------------- #
# Event-log heuristics
# --------------------------------------------------------------------- #

# Build indicators in Bash commands.
_BUILD_CMD_RE = re.compile(
    r"\b(?:cmake|make\s+-?\w*|ninja|meson|\./configure|\./autogen|"
    r"secb\s+build|compile|cargo\s+build)\b",
    re.IGNORECASE,
)

# PoC / reproduction indicators in Bash commands.
_POC_CMD_RE = re.compile(
    r"\b(?:secb\s+repro|valgrind|asan|sanitizer|"
    r"generate_poc|\./fuzz|/testcase/poc|\./poc)\b",
    re.IGNORECASE,
)

# Patch creation indicators in Bash commands.
_PATCH_CMD_RE = re.compile(
    r"(?:git\s+diff\b.*>.*\.patch|git\s+format-patch|"
    r"\.patch\s*$|/testcase/.*\.patch|secb\s+patch|"
    r"model_patch\.diff)",
    re.IGNORECASE,
)

# Source-tree paths count as real source edits. /src/ is the canonical
# upstream clone; /tmp/<project-like-name>/ is the flat-CLI pattern
# (the CLI often re-clones under /tmp/<name>/). Build scratch dirs
# (CMakeFiles, *_build/, build/) and generated headers (exv_conf.h)
# are excluded: those are compiler output, not source patches.
# Match /src/<project>/... (canonical upstream) or /tmp/<something>/...
# whose first directory component under /tmp/ starts with one of the
# CVE project prefixes (njs, mruby, faad2, gpac, openjpeg, exiv2,
# imagemagick). Trailing name may be followed by arbitrary suffix like
# ``-build-clean`` or ``_asan``.
_SOURCE_EDIT_RE = re.compile(
    r"^(?:/src/[^/]+/|"
    r"/tmp/(?:njs|mruby|faad2|gpac|openjpeg|exiv2|imagemagick)[^/]*/)",
    re.IGNORECASE,
)
_BUILD_SCRATCH_RE = re.compile(
    r"(?:CMakeFiles|CMakeCache\.txt|_build/|/build/|/exv_conf\.h|"
    r"\.ninja_|\.o\.d$|flags\.make$|\.o$|\.cmake$)",
    re.IGNORECASE,
)

@dataclass(frozen=True, slots=True)
class EventFlags:
    build: bool
    poc: bool
    patch: bool
    report: bool


def _scan_events(events_path: Path) -> EventFlags:
    if not events_path.exists():
        return EventFlags(False, False, False, False)
    build = poc = patch = report = False

    with events_path.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = e.get("event_type")
            payload = e.get("payload", {}) or {}

            # NOTE: child_spawned alone is NOT evidence of a phase
            # being *reached* -- it only proves the BOSS tried. The
            # deliverable evidence must come from workspace files or
            # from work_completed text.

            # B-cell work_completed summary -- look for phase keywords.
            if et == "work_completed":
                data = payload.get("data", {}) or {}
                result = data.get("result", "") or ""
                rlc = result.lower()
                # Build: any mention of successful build / instrumented
                # binary / ASan-linked build. "build complete" is the
                # B2-Builder exit phrase; B1 runs use free-form wording
                # like "built successfully", "ASan-enabled build",
                # "ASan binary", "reproduction confirmed", etc.
                if re.search(
                    r"build\s+(?:complete|success|finished|confirm|done|"
                    r"ok|passed)|"
                    r"asan[-\s]*(?:instrument|enabled|binary|build)|"
                    r"built\s+(?:with|binary|the|successfully)|"
                    r"reproduction\s+(?:confirmed|reproduced)|"
                    r"crash[-\s]*confirmed|confirmed\s+crash|"
                    r"compiled\s+(?:successfully|with)|"
                    r"asan\s+(?:reports|binary|crash)",
                    rlc,
                ):
                    build = True
                if re.search(
                    r"\bpoc\b|reproduc|asan.*crash|exploit|sanitizer.*error|"
                    r"crash\s+confirmed|confirmed\s+crash",
                    rlc,
                ):
                    poc = True
                if re.search(
                    r"model_patch\.diff|fix\.patch|patch\s+(?:applied|created|written)|"
                    r"fix\s+applied|patch\s+verified",
                    rlc,
                ):
                    patch = True
                if re.search(
                    r"security_report\.md|report\.md|summary\.md|root.cause\.md|"
                    r"findings\.md",
                    rlc,
                ):
                    report = True

            # Flat A-cell tool_use events.
            if et == "tool_use":
                tool = payload.get("tool_name")
                tool_input = payload.get("tool_input", {}) or {}
                if not isinstance(tool_input, dict):
                    continue

                if tool == "Bash":
                    cmd = tool_input.get("command", "") or ""
                    if _BUILD_CMD_RE.search(cmd):
                        build = True
                    if _POC_CMD_RE.search(cmd):
                        poc = True
                    if _PATCH_CMD_RE.search(cmd):
                        patch = True
                elif tool == "Write":
                    fp = tool_input.get("file_path", "") or ""
                    lc = fp.lower()
                    name = fp.rsplit("/", 1)[-1]
                    if _REPORT_NAME_RE.match(name):
                        report = True
                    if ".patch" in lc or "model_patch.diff" in lc or "fix.patch" in lc:
                        patch = True
                    if _POC_NAME_RE.match(name):
                        poc = True
                elif tool == "Edit":
                    fp = tool_input.get("file_path", "") or ""
                    if _SOURCE_EDIT_RE.search(fp) and not _BUILD_SCRATCH_RE.search(fp):
                        patch = True
    return EventFlags(build, poc, patch, report)


# --------------------------------------------------------------------- #
# Run resolution + union
# --------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class PhaseReached:
    build: bool
    poc: bool
    patch: bool
    report: bool

    @classmethod
    def union(cls, a: WorkspaceFlags, b: EventFlags) -> "PhaseReached":
        return cls(
            build=a.build or b.build,
            poc=a.poc or b.poc,
            patch=a.patch or b.patch,
            report=a.report or b.report,
        )


def run_dir_for(cve_id: str, cell: str) -> Path:
    if cell in ("A1", "A2"):
        return V1_RUNS / cve_id / cell / "0"
    return V2_RUNS / cve_id / cell / "0"


def phase_for_run(cve_id: str, cell: str) -> tuple[PhaseReached, WorkspaceFlags, EventFlags]:
    rd = run_dir_for(cve_id, cell)
    if cell in ("A1", "A2"):
        ws = scan_a_workspace(cve_id, cell)
    else:
        ws = scan_b_workspace(cve_id, cell)
    ev = _scan_events(rd / "events.jsonl")
    return PhaseReached.union(ws, ev), ws, ev


# --------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------- #

def _short_cve(cve: str) -> str:
    project, _, rest = cve.partition(".")
    return f"{project}:{rest.removeprefix('cve-')}"


def build_matrix() -> dict[str, dict[str, PhaseReached]]:
    matrix: dict[str, dict[str, PhaseReached]] = {}
    for cell in CELLS:
        matrix[cell] = {}
        for cve in PRIMARY_CVES:
            pr, _, _ = phase_for_run(cve, cell)
            matrix[cell][cve] = pr
    return matrix


def render_summary_table(matrix: dict[str, dict[str, PhaseReached]]) -> str:
    header = "| cell | Build | PoC | Patch | Report |"
    sep = "|---|---|---|---|---|"
    lines = [header, sep]
    for cell in CELLS:
        row = [cell]
        for phase in PHASES:
            n = sum(1 for pr in matrix[cell].values() if getattr(pr, phase))
            row.append(f"{n} / 10")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def render_per_cve_table(matrix: dict[str, dict[str, PhaseReached]], cell: str) -> str:
    header = "| CVE | Build | PoC | Patch | Report |"
    sep = "|---|---|---|---|---|"
    lines = [f"### {cell}", "", header, sep]
    for cve in PRIMARY_CVES:
        pr = matrix[cell][cve]
        cells_out = [
            "Y" if pr.build else ".",
            "Y" if pr.poc else ".",
            "Y" if pr.patch else ".",
            "Y" if pr.report else ".",
        ]
        lines.append("| " + _short_cve(cve) + " | " + " | ".join(cells_out) + " |")
    return "\n".join(lines)


def main() -> None:
    matrix = build_matrix()

    print("## Phase-reached summary (of 10 primary CVE runs per cell)\n")
    print(render_summary_table(matrix))
    print()

    print("## Per-CVE breakdown\n")
    for cell in CELLS:
        print(render_per_cve_table(matrix, cell))
        print()

    print("## Sanity: union source breakdown\n")
    print(
        "Each flag may be set by the workspace scan, the event scan, or both. "
        "The table below counts how often each signal alone fires per cell."
    )
    print()
    header = "| cell | build (WS/EV) | poc (WS/EV) | patch (WS/EV) | report (WS/EV) |"
    sep = "|---|---|---|---|---|"
    print(header)
    print(sep)
    for cell in CELLS:
        counts = {p: [0, 0] for p in PHASES}  # [workspace, events]
        for cve in PRIMARY_CVES:
            _, ws, ev = phase_for_run(cve, cell)
            for phase, wflag, eflag in (
                ("build", ws.build, ev.build),
                ("poc", ws.poc, ev.poc),
                ("patch", ws.patch, ev.patch),
                ("report", ws.report, ev.report),
            ):
                if wflag:
                    counts[phase][0] += 1
                if eflag:
                    counts[phase][1] += 1
        row = [cell] + [f"{counts[p][0]}/{counts[p][1]}" for p in PHASES]
        print("| " + " | ".join(row) + " |")


if __name__ == "__main__":
    main()
