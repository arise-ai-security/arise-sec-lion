#!/usr/bin/env python3
"""List every expected built executable per B1 CVE instance.

Source of truth: ./runs (filter study_id == "b1-batch-autogen", dedupe by task).
Success criterion: at least one expected executable found anywhere under the
run dir (either work/bin or the source/build tree the builder produced).
"""

import json
import os
from pathlib import Path

EXPECTED = {
    "gpac": {"MP4Box", "MP4Client", "gpac"},
    "openjpeg": {"opj_compress", "opj_decompress", "opj_dump"},
    "mruby": {"mruby", "mirb", "mrbc", "mruby-strip"},
    "jq": {"jq"},
    "libxls": {"xls2csv"},
    "exiv2": {"exiv2"},
    "faad2": {"faad"},
    "njs": {"njs"},
    "libredwg": {"dwgread", "dwgwrite", "dwg2dxf", "dwg2SVG", "dxf2dwg",
                 "dwgbmp", "dwgfilter", "dwggrep", "dwglayers", "dwgrewrite",
                 "dwgadd", "dwg_ps"},
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
    "openexr": {"exrenvmap", "exrheader", "exrmaketiled", "exrmultipart",
                "exrmultiview", "exrstdattr", "exrcheck"},
    "libmodbus": {"unit-test-server", "unit-test-client", "random-test-server",
                  "bandwidth-server-one", "bandwidth-client"},
    "libiec61850": {"server_example_basic_io", "client_example_basic_io",
                    "server_example_61400_25"},
    "libdwarf": {"dwarfdump"},
    "php": {"php", "php-cgi"},
}

TOOLCHAIN = {
    "afl-fuzz", "afl-cc", "afl-gcc", "afl-g++", "afl-clang", "afl-clang-fast",
    "afl-clang-fast++", "afl-clang-lto", "afl-clang-lto++", "afl-as",
    "afl-cmin", "afl-tmin", "afl-showmap", "afl-analyze", "afl-system-config",
    "honggfuzz", "hfuzz-cc", "hfuzz-clang", "hfuzz-gcc", "hfuzz-clang++",
    "hfuzz-g++", "libfuzzer", "fuzztest", "llvm-symbolizer", "compiler_rt",
}

SKIP_DIRS = {".git", "aflplusplus", "fuzztest", "honggfuzz", "libfuzzer",
             "node_modules", "__pycache__"}

RANK = {"success": 0, "failed": 1, "timeout": 2}


def find_all(run_dir: Path, expected: set[str]) -> list[Path]:
    """Walk run_dir; return every executable file whose basename is in expected."""
    hits = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name in TOOLCHAIN or name not in expected:
                continue
            p = Path(root) / name
            try:
                if p.is_file() and (p.lstat().st_mode & 0o111):
                    hits.append(p)
            except OSError:
                pass
    return hits


def collect() -> list[tuple[Path, dict]]:
    """All B1 runs, deduplicated by task (best exit_status, then most recent)."""
    best: dict[str, tuple[Path, dict]] = {}
    for child in sorted(Path("runs").iterdir()):
        mf = child / "run_manifest.json"
        if not mf.exists():
            continue
        m = json.loads(mf.read_text())
        if m.get("study_id") != "b1-batch-autogen":
            continue
        task = m["task"]
        cur = best.get(task)
        if cur is None:
            best[task] = (child, m)
            continue
        cur_rank = RANK.get(cur[1]["exit_status"], 99)
        new_rank = RANK.get(m["exit_status"], 99)
        if (new_rank, m["started_at"]) < (cur_rank, cur[1]["started_at"]):
            # lower rank wins; tie -> later started_at wins (so flip with > for time)
            best[task] = (child, m)
        elif new_rank < cur_rank:
            best[task] = (child, m)
        elif new_rank == cur_rank and m["started_at"] > cur[1]["started_at"]:
            best[task] = (child, m)
    return sorted(best.values(), key=lambda x: x[1]["task"])


def rel(p: Path, run_dir: Path) -> str:
    return str(p.relative_to(run_dir))


def main() -> None:
    runs = collect()
    rows = []
    for run_dir, m in runs:
        task = m["task"]
        project = task.split(".", 1)[0]
        expected = EXPECTED.get(project, set())
        hits = find_all(run_dir, expected) if expected else []
        locations = sorted({rel(h, run_dir) for h in hits})
        rows.append((task, m["exit_status"], len(locations), locations))

    # Markdown table
    print("| cve_instance_id | exit_status | # built | locations (relative to run dir) |")
    print("|---|---|---:|---|")
    for task, status, n, locs in rows:
        cell = "<br>".join(locs) if locs else "—"
        print(f"| {task} | {status} | {n} | {cell} |")

    # Summary line
    strict = sum(1 for r in rows if r[2] > 0)
    print(f"\nTotal unique B1 tasks: {len(rows)}; STRICT (≥1 built): {strict}")

    # CSV next to it for re-use
    out = Path("/tmp/b1_builder_locations.csv")
    with out.open("w") as f:
        f.write("cve_instance_id,exit_status,num_built,locations\n")
        for task, status, n, locs in rows:
            f.write(f'{task},{status},{n},"{ "; ".join(locs) }"\n')
    print(f"CSV: {out}")


if __name__ == "__main__":
    main()
