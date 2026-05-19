#!/usr/bin/env python3
"""A1/A2 Fix-Validator FP/FN/TP/TN sweep.

Mechanical first pass of the audit described in
``experiments/a12-batch-autogen/scripts/AUDIT_PROMPT.md``.

Walks ``runs/``, keeps directories whose ``run_manifest.json`` has
``study_id`` in the configured set (defaults cover both ``a12-batch-autogen``
and ``a12-batch-autogen-2026-05-16``) AND ``cell in {A1, A2}``. Deduplicates
by ``(cell, task)`` so A1 and A2 are treated as independent treatments.

For each unique (cell, task):
  - parses the patch_validation_results.txt and security_report.md and
    classifies the claim as PASS / FAIL / AMBIGUOUS / MISSING (heuristic
    PASS/FAIL token matching — see *_RE constants below).
  - walks every text file in testcase/ and looks for an ASan/MSan/UBSan
    ERROR marker, bucketing the file as ``post_patch_san_files``,
    ``pre_patch_san_files`` or ``other_san_files`` based on filename hints.

Outputs:
  - stdout: claim x evidence summary, per-run FP candidate listing,
    per-run "claim PASS with no evidence" listing.
  - JSON: ``/tmp/a12_evidence_sweep.json`` — one record per unique
    (cell, task) pair with all the fields used by the LLM deep-dive step.

Run from repo root: ``python3 experiments/a12-batch-autogen/scripts/audit_fix_validator.py``
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

DEFAULT_STUDY_IDS = (
    "a12-batch-autogen",
    "a12-batch-autogen-2026-05-16",
)
DEFAULT_CELLS = ("A1", "A2")
RANK = {"success": 0, "failed": 1, "timeout": 2}

# ---------- evidence regexes ----------
SAN_ERROR_RE = re.compile(
    r"==\d+==ERROR:|"
    r"^SUMMARY:\s*(AddressSanitizer|UndefinedBehaviorSanitizer|"
    r"MemorySanitizer|ThreadSanitizer|LeakSanitizer)|"
    r"runtime error:|"
    r"AddressSanitizer:|LeakSanitizer:|"
    r"UndefinedBehaviorSanitizer:|MemorySanitizer:",
    re.MULTILINE,
)

POST_PATCH_NAME_RE = re.compile(
    r"(after[._-]?patch|post[._-]?patch|patched[._-]?(run|test|output|"
    r"asan|stdout|stderr|valgrind)|fix[._-]?(run|test|attempt|verify)|"
    r"patch[._-]?(test|repro|validation_repro|verify|quick|run|output)|"
    r"validation[._-]?repro|validation[._-]?after|repro[._-]?after|"
    r"asan[._-]?after|asan[._-]?post|UNIFIED_patched_test|"
    r"gdb[._-]?after|msan[._-]?post)",
    re.IGNORECASE,
)
PRE_PATCH_NAME_RE = re.compile(
    r"(before[._-]?patch|baseline|repro_run|poc[._-]?(run|output|test)|"
    r"exploit[._-]?validation|exploit[._-]?validator|asan[._-]?before|"
    r"UNIFIED_baseline)",
    re.IGNORECASE,
)

# ---------- claim regexes ----------
VERDICT_PASS_RE = re.compile(r"^\s*VERDICT:\s*PASS\b", re.IGNORECASE | re.MULTILINE)
VERDICT_FAIL_RE = re.compile(r"^\s*VERDICT:\s*FAIL\b", re.IGNORECASE | re.MULTILINE)
RESULT_PASS_RE = re.compile(
    r"(ALL\s+TESTS?\s+PASSED|ALL\s+VALIDATIONS?\s+PASS|"
    r"VALIDATION\s+SUMMARY[^\n]{0,40}?\bPASS\b|"
    r"OVERALL\s*[:=]\s*PASS|FINAL\s+VERDICT[^\n]{0,40}?\bPASS\b|"
    r"RESULT\s*[:=]\s*PASS|\bPATCH\s+VALIDATED\b|"
    r"\bSUCCESS(?:FULLY)?\s+(?:patched|fixed|validated|verified)\b|"
    r"\bAPPROVED\s+FOR\s+DEPLOYMENT\b|"
    r"\bREADY\s+FOR\s+DEPLOYMENT\b|"
    r"\bVALIDATION\s+COMPLETE\b|"
    r"\bVULNERABILITY\s+(?:ELIMINATED|REMEDIATED)\b)",
    re.IGNORECASE,
)
RESULT_FAIL_RE = re.compile(
    r"(ALL\s+TESTS?\s+FAILED|VALIDATION\s+SUMMARY[^\n]{0,40}?\bFAIL\b|"
    r"OVERALL\s*[:=]\s*FAIL|FINAL\s+VERDICT[^\n]{0,40}?\bFAIL\b|"
    r"RESULT\s*[:=]\s*FAIL|\bPATCH\s+NOT\s+VALIDATED\b|"
    r"\bDID\s+NOT\s+(?:fix|patch|resolve)\b|"
    r"\bSTILL\s+(?:crashes|triggers|vulnerable)\b)",
    re.IGNORECASE,
)
REPORT_SUCCESS_RE = re.compile(
    r"\b(successfully\s+patched|patch\s+(?:is\s+)?validated|"
    r"vulnerability\s+(?:is\s+)?(?:fixed|patched|resolved|mitigated|"
    r"eliminated|remediated)|"
    r"all\s+phases\s+completed|both\s+(?:verdicts?|validations?)\s*[:=]?\s*PASS|"
    r"fix\s+(?:verified|validated)|fully\s+fixed|fix\s+is\s+complete|"
    r"patch\s+effectively\s+(?:removes|eliminates)|"
    r"approved\s+for\s+deployment|ready\s+for\s+deployment)",
    re.IGNORECASE,
)

PATCH_V_NAMES = (
    "patch_validation_results.txt", "patch_validation_results.md",
    "patch_validation_report.md", "patch_validation_report.txt",
    "patch_validation_summary.txt", "patch_validation_summary.md",
    "patch_validation.txt", "patch_validation.md",
    "FINAL_VALIDATION_REPORT.md", "final_validation_summary.txt",
    "final_validation.txt",
)


def collect_runs(runs_root: Path, study_ids: set[str],
                 cells: set[str]) -> list[tuple[Path, dict]]:
    """Pick all (cell, task) pairs matching the filter; dedupe each by
    best exit_status, tie-broken by most-recent started_at.
    """
    best: dict[tuple[str, str], tuple[Path, dict]] = {}
    for child in sorted(runs_root.iterdir()):
        mf = child / "run_manifest.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
        except json.JSONDecodeError:
            continue
        if m.get("study_id") not in study_ids:
            continue
        cell = m.get("cell", "")
        if cells and cell not in cells:
            continue
        task = m.get("task", "")
        if not task:
            continue
        key = (cell, task)
        cur = best.get(key)
        if cur is None:
            best[key] = (child, m)
            continue
        cur_rank = RANK.get(cur[1].get("exit_status", ""), 99)
        new_rank = RANK.get(m.get("exit_status", ""), 99)
        if new_rank < cur_rank:
            best[key] = (child, m)
        elif new_rank == cur_rank and m.get("started_at", "") > cur[1].get("started_at", ""):
            best[key] = (child, m)
    return sorted(best.values(), key=lambda x: (x[1].get("cell", ""), x[1].get("task", "")))


def read_text(p: Path | None) -> str:
    if not p:
        return ""
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def classify_claim(text: str, is_report: bool) -> str:
    if not text:
        return "MISSING"
    if VERDICT_FAIL_RE.search(text):
        return "FAIL"
    if VERDICT_PASS_RE.search(text):
        return "PASS"
    pass_re = REPORT_SUCCESS_RE if is_report else RESULT_PASS_RE
    fail_re = RESULT_FAIL_RE
    has_pass = bool(pass_re.search(text))
    has_fail = bool(fail_re.search(text))
    if has_pass and not has_fail:
        return "PASS"
    if has_fail and not has_pass:
        return "FAIL"
    return "AMBIGUOUS"


def first_existing(testcase: Path, names: tuple[str, ...]) -> Path | None:
    for n in names:
        p = testcase / n
        if p.is_file():
            return p
    return None


def scan_testcase(testcase: Path) -> dict:
    if not testcase.is_dir():
        return {"post_patch_san": [], "pre_patch_san": [], "all_san": []}
    post: list[str] = []
    pre: list[str] = []
    all_san: list[str] = []
    skip_suffix = {".bin", ".tiff", ".pgx", ".wav", ".pdf",
                   ".png", ".jpg", ".mp4", ".mat"}
    for p in sorted(testcase.iterdir()):
        if not p.is_file():
            continue
        if p.stat().st_size > 5 * 1024 * 1024:
            continue
        if p.suffix.lower() in skip_suffix:
            continue
        try:
            txt = p.read_text(errors="replace")
        except (OSError, UnicodeError):
            continue
        if not SAN_ERROR_RE.search(txt):
            continue
        all_san.append(p.name)
        if POST_PATCH_NAME_RE.search(p.name):
            post.append(p.name)
        elif PRE_PATCH_NAME_RE.search(p.name):
            pre.append(p.name)
    return {"post_patch_san": post, "pre_patch_san": pre, "all_san": all_san}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs-root", default="runs",
                        help="path to the runs/ directory (default: ./runs)")
    parser.add_argument("--study-id", action="append", default=None,
                        help=f"study_id to include; repeatable. "
                             f"Defaults: {', '.join(DEFAULT_STUDY_IDS)}")
    parser.add_argument("--cell", action="append", default=None,
                        help=f"cell to include; repeatable. "
                             f"Defaults: {', '.join(DEFAULT_CELLS)}")
    parser.add_argument("--out-json", default="/tmp/a12_evidence_sweep.json")
    args = parser.parse_args()

    runs_root = Path(args.runs_root).resolve()
    study_ids = set(args.study_id) if args.study_id else set(DEFAULT_STUDY_IDS)
    cells = set(args.cell) if args.cell else set(DEFAULT_CELLS)

    runs = collect_runs(runs_root, study_ids, cells)
    rows = []
    for run_dir, m in runs:
        testcase = run_dir / "testcase"
        patch_v_path = first_existing(testcase, PATCH_V_NAMES)
        patch_claim = classify_claim(read_text(patch_v_path), is_report=False)
        report_path = testcase / "security_report.md"
        report_claim = classify_claim(
            read_text(report_path) if report_path.is_file() else "",
            is_report=True)
        ev = scan_testcase(testcase)
        rows.append({
            "cell": m.get("cell", ""),
            "task": m["task"],
            "run_id": run_dir.name,
            "study_id": m.get("study_id", ""),
            "exit_status": m.get("exit_status", ""),
            "patch_claim": patch_claim,
            "report_claim": report_claim,
            "patch_v_file": patch_v_path.name if patch_v_path else "",
            "post_patch_san_files": ev["post_patch_san"],
            "pre_patch_san_files": ev["pre_patch_san"],
            "other_san_files": [n for n in ev["all_san"]
                                if n not in ev["post_patch_san"]
                                and n not in ev["pre_patch_san"]],
        })

    print(f"runs_root: {runs_root}")
    print(f"study_ids: {sorted(study_ids)}")
    print(f"cells:     {sorted(cells)}")
    print(f"Audited {len(rows)} deduped (cell, task) pairs\n")

    print("Per-cell breakdown:")
    for c in sorted(cells):
        n_total = sum(1 for r in rows if r["cell"] == c)
        n_pass = sum(1 for r in rows if r["cell"] == c and r["patch_claim"] == "PASS")
        n_post = sum(1 for r in rows if r["cell"] == c and r["post_patch_san_files"])
        n_fp = sum(1 for r in rows
                   if r["cell"] == c and r["post_patch_san_files"]
                   and (r["patch_claim"] == "PASS" or r["report_claim"] == "PASS"))
        print(f"  {c}: n={n_total}  patch_PASS={n_pass}  "
              f"post_patch_san={n_post}  FP_candidates={n_fp}")
    print()

    print("=" * 100)
    print("FP CANDIDATES (patch_claim==PASS or report_claim==PASS, but a "
          "post-patch sanitizer file exists)")
    print("=" * 100)
    for r in sorted(rows, key=lambda x: (x["cell"], x["task"])):
        if not r["post_patch_san_files"]:
            continue
        if r["patch_claim"] != "PASS" and r["report_claim"] != "PASS":
            continue
        print(f"\n[{r['cell']}]  {r['task']}  ({r['run_id'][:12]}, "
              f"exit={r['exit_status']})")
        print(f"  patch_claim={r['patch_claim']}  report_claim={r['report_claim']}  "
              f"patch_v_file={r['patch_v_file']!r}")
        print(f"  post-patch ASan files: {r['post_patch_san_files']}")
        if r["pre_patch_san_files"]:
            print(f"  pre-patch ASan files (context): "
                  f"{r['pre_patch_san_files'][:5]}")

    print()
    print("=" * 100)
    print("FN CANDIDATES (patch_claim != PASS and report_claim != PASS, but at")
    print("least one post-patch evidence file with NO sanitizer ERROR exists)")
    print("=" * 100)
    for r in sorted(rows, key=lambda x: (x["cell"], x["task"])):
        if r["patch_claim"] == "PASS" or r["report_claim"] == "PASS":
            continue
        if r["post_patch_san_files"]:
            continue
        # The shallow sweep only flags this; the LLM deep-dive must
        # actually look at the run dir to confirm clean post-patch evidence.
        print(f"  [{r['cell']}]  {r['task']}  ({r['run_id'][:12]})  "
              f"patch={r['patch_claim']} report={r['report_claim']} "
              f"patch_v_file={r['patch_v_file']!r}")

    print()
    print("=" * 100)
    print("Claim=PASS but NO sanitizer evidence at all "
          "(likely skipped validation entirely)")
    print("=" * 100)
    for r in sorted(rows, key=lambda x: (x["cell"], x["task"])):
        if r["patch_claim"] != "PASS" and r["report_claim"] != "PASS":
            continue
        if r["post_patch_san_files"] or r["pre_patch_san_files"] or r["other_san_files"]:
            continue
        print(f"  [{r['cell']}]  {r['task']}  ({r['run_id'][:12]})  "
              f"patch={r['patch_claim']} report={r['report_claim']}")

    out = Path(args.out_json)
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nFull JSON: {out}")


if __name__ == "__main__":
    main()
