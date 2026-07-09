#!/usr/bin/env python3
"""B1 Builder Real Success verifier.

Walks runs/ looking for run_manifest.json with study_id == "b1-batch-autogen",
deduplicates by task (instance id), and checks whether the expected project
binary was built for each run.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

EXPECTED: dict[str, set[str]] = {
    "gpac": {"MP4Box", "MP4Client", "gpac"},
    "openjpeg": {"opj_compress", "opj_decompress", "opj_dump"},
    "mruby": {"mruby", "mirb", "mrbc", "mruby-strip"},
    "jq": {"jq"},
    "libxls": {"xls2csv"},
    "exiv2": {"exiv2"},
    "faad2": {"faad"},
    "njs": {"njs"},
    "libredwg": {
        "dwgread", "dwgwrite", "dwg2dxf", "dwg2SVG", "dxf2dwg", "dwgbmp",
        "dwgfilter", "dwggrep", "dwglayers", "dwgrewrite", "dwgadd", "dwg_ps",
    },
    "matio": {"matdump"},
    "libsndfile": {"sndfile-info", "sndfile-convert", "sndfile-play", "sndfile-cmp"},
    "libheif": {"heif-convert", "heif-info", "heif-enc"},
    "libjpeg-turbo": {"cjpeg", "djpeg", "jpegtran", "rdjpgcom", "wrjpgcom"},
    "upx": {"upx"},
    "md4c": {"md2html"},
    "liblouis": {"lou_translate", "lou_checktable", "lou_debug", "lou_trace"},
    "libarchive": {"bsdtar", "bsdcpio"},
    "libplist": {"plistutil"},
    "readstat": {"readstat"},
    "qpdf": {"qpdf"},
    "openexr": {
        "exrenvmap", "exrheader", "exrmaketiled", "exrmultipart",
        "exrmultiview", "exrstdattr", "exrcheck",
    },
    "libmodbus": {
        "unit-test-server", "unit-test-client", "random-test-server",
        "bandwidth-server-one", "bandwidth-client",
    },
    "libiec61850": {
        "server_example_basic_io", "client_example_basic_io",
        "server_example_61400_25",
    },
    "libdwarf": {"dwarfdump"},
    "php": {"php", "php-cgi"},
}

TOOLCHAIN: set[str] = {
    "afl-fuzz", "afl-cc", "afl-gcc", "afl-g++", "afl-clang", "afl-clang-fast",
    "afl-clang-fast++", "afl-clang-lto", "afl-clang-lto++", "afl-as",
    "afl-cmin", "afl-tmin", "afl-showmap", "afl-analyze", "afl-system-config",
    "honggfuzz", "hfuzz-cc", "hfuzz-clang", "hfuzz-gcc", "hfuzz-clang++",
    "hfuzz-g++", "libfuzzer", "fuzztest", "llvm-symbolizer", "compiler_rt",
}

SKIP_DIRS: set[str] = {
    ".git", "aflplusplus", "fuzztest", "honggfuzz", "libfuzzer",
    "node_modules", "__pycache__",
}

STUDY_ID = "b1-batch-autogen"


@dataclass
class Verdict:
    run_id: str
    task: str
    project: str
    exit_status: str
    verdict: str  # STRICT | NONE | SKIPPED
    path_where_found: str
    started_at: str


def find_first_binary(run_dir: Path, expected: set[str]) -> str | None:
    for root, dirs, files in os.walk(run_dir):
        # prune
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in TOOLCHAIN:
                continue
            if name not in expected:
                continue
            full = Path(root) / name
            try:
                st = full.lstat()
            except OSError:
                continue
            if not (st.st_mode & 0o111):
                continue
            # ensure it's a regular file (not a symlink to nowhere or dir)
            try:
                if not full.is_file():
                    continue
            except OSError:
                continue
            return str(full.resolve())
    return None


def load_manifest(run_dir: Path) -> dict | None:
    mf = run_dir / "run_manifest.json"
    if not mf.exists():
        return None
    try:
        return json.loads(mf.read_text())
    except json.JSONDecodeError:
        return None


def collect_b1_runs(runs_root: Path) -> list[tuple[Path, dict]]:
    out: list[tuple[Path, dict]] = []
    for child in sorted(runs_root.iterdir()):
        if not child.is_dir():
            continue
        m = load_manifest(child)
        if m is None:
            continue
        if m.get("study_id") != STUDY_ID:
            continue
        out.append((child, m))
    return out


def dedupe_by_task(runs: list[tuple[Path, dict]]) -> list[tuple[Path, dict]]:
    """Keep one run per task. Prefer exit_status=success, then most recent started_at."""
    rank = {"success": 0, "failed": 1, "timeout": 2}
    best: dict[str, tuple[Path, dict]] = {}
    for path, m in runs:
        task = m.get("task", "")
        if not task:
            continue
        cur = best.get(task)
        if cur is None:
            best[task] = (path, m)
            continue
        cur_m = cur[1]
        cur_rank = rank.get(cur_m.get("exit_status", ""), 99)
        new_rank = rank.get(m.get("exit_status", ""), 99)
        if new_rank < cur_rank:
            best[task] = (path, m)
        elif new_rank == cur_rank:
            if m.get("started_at", "") > cur_m.get("started_at", ""):
                best[task] = (path, m)
    return list(best.values())


def evaluate(runs: Iterable[tuple[Path, dict]]) -> list[Verdict]:
    out: list[Verdict] = []
    for path, m in runs:
        task = m.get("task", "")
        project = task.split(".", 1)[0] if task else ""
        exit_status = m.get("exit_status", "unknown")
        started_at = m.get("started_at", "")
        run_id = m.get("run_id", path.name)
        if project not in EXPECTED:
            out.append(Verdict(run_id, task, project, exit_status,
                               "SKIPPED", "", started_at))
            continue
        hit = find_first_binary(path, EXPECTED[project])
        if hit is not None:
            out.append(Verdict(run_id, task, project, exit_status,
                               "STRICT", hit, started_at))
        else:
            out.append(Verdict(run_id, task, project, exit_status,
                               "NONE", "", started_at))
    return out


def render_table(verdicts: list[Verdict]) -> str:
    rows = [("run_id", "task", "project", "exit_status",
             "verdict", "path_where_found")]
    for v in verdicts:
        rows.append((v.run_id[:12], v.task, v.project,
                     v.exit_status, v.verdict, v.path_where_found))
    widths = [max(len(str(r[i])) for r in rows) for i in range(6)]
    lines = []
    for r in rows:
        lines.append("  ".join(str(r[i]).ljust(widths[i]) for i in range(6)))
    return "\n".join(lines)


def summary(verdicts: list[Verdict]) -> str:
    n = len(verdicts)
    strict = [v for v in verdicts if v.verdict == "STRICT"]
    none = [v for v in verdicts if v.verdict == "NONE"]
    skipped = [v for v in verdicts if v.verdict == "SKIPPED"]
    lines = [
        f"Total runs analyzed: {n}",
        f"STRICT: {len(strict)} / {n} ({(len(strict)/n*100 if n else 0):.1f}%)",
        f"NONE:   {len(none)} / {n} ({(len(none)/n*100 if n else 0):.1f}%)",
        f"SKIPPED:{len(skipped)} / {n}",
    ]
    matrix: dict[str, dict[str, int]] = {}
    for v in verdicts:
        matrix.setdefault(v.exit_status, {"STRICT": 0, "NONE": 0, "SKIPPED": 0})
        matrix[v.exit_status][v.verdict] += 1
    lines.append("")
    lines.append("exit_status x verdict matrix:")
    header = f"  {'exit_status':<12}  {'STRICT':>7}  {'NONE':>7}  {'SKIPPED':>8}  {'TOTAL':>7}"
    lines.append(header)
    for es, counts in sorted(matrix.items()):
        total = sum(counts.values())
        lines.append(
            f"  {es:<12}  {counts['STRICT']:>7}  {counts['NONE']:>7}  "
            f"{counts['SKIPPED']:>8}  {total:>7}"
        )
    return "\n".join(lines)


def samples(verdicts: list[Verdict], runs_root: Path) -> str:
    lines = ["Sample STRICT paths (up to 5):"]
    for v in [x for x in verdicts if x.verdict == "STRICT"][:5]:
        lines.append(f"  {v.run_id[:12]}  {v.task:<30}  {v.path_where_found}")
    lines.append("")
    lines.append("Sample NONE work/bin/ listings (up to 5):")
    for v in [x for x in verdicts if x.verdict == "NONE"][:5]:
        wbin = runs_root / v.run_id / "work" / "bin"
        if wbin.exists():
            entries = sorted(p.name for p in wbin.iterdir())
            shown = ", ".join(entries[:12]) or "(empty)"
            lines.append(f"  {v.run_id[:12]}  {v.task:<30}  work/bin/ -> {shown}")
        else:
            lines.append(f"  {v.run_id[:12]}  {v.task:<30}  work/bin/ MISSING")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", default="runs")
    parser.add_argument("--out-json", default="/tmp/builder_strict_verdicts.json")
    args = parser.parse_args()

    runs_root = Path(args.runs_root).resolve()
    all_b1 = collect_b1_runs(runs_root)
    raw_verdicts = evaluate(all_b1)
    deduped = dedupe_by_task(all_b1)
    deduped.sort(key=lambda x: x[1].get("task", ""))
    verdicts = evaluate(deduped)

    print(f"Discovered {len(all_b1)} B1 runs in {runs_root}; "
          f"{len(deduped)} unique tasks after dedup "
          f"(study_id == \"{STUDY_ID}\").\n")

    print("=" * 80)
    print("DEDUPLICATED VIEW (one run per task, prefer success > failed > timeout,")
    print("                  then most-recent started_at)")
    print("=" * 80)
    print(render_table(verdicts))
    print()
    print(summary(verdicts))
    print()
    print(samples(verdicts, runs_root))

    print()
    print("=" * 80)
    print("RAW VIEW (every B1 run, no dedup — for direct comparison to 124-run prediction)")
    print("=" * 80)
    print(summary(raw_verdicts))

    out_path = Path(args.out_json)
    out_path.write_text(json.dumps({
        "deduped": [asdict(v) for v in verdicts],
        "raw": [asdict(v) for v in raw_verdicts],
    }, indent=2))
    print(f"\nWrote per-run JSON: {out_path}")


if __name__ == "__main__":
    main()
