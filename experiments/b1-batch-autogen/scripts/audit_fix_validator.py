#!/usr/bin/env python3
"""Broad-name post-patch evidence sweep.

For every deduped B1 run, scan every file in testcase/ whose name suggests it
was produced AFTER the patch was applied. If any such file contains a
sanitizer ERROR signature, the fix has not actually worked. Cross-check
against patch_validation file's claimed verdict + security_report claim.
"""

import json
import re
from pathlib import Path

RANK = {"success": 0, "failed": 1, "timeout": 2}

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
    r"asan|stdout|stderr)|fix[._-]?(run|test|attempt|verify)|"
    r"patch[._-]?(test|repro|validation|verify|quick|run|output)|"
    r"validation[._-]?repro|gdb[._-]?after|asan[._-]?after|"
    r"msan[._-]?post|asan[._-]?post(?!_check))",
    re.IGNORECASE,
)

PRE_PATCH_NAME_RE = re.compile(
    r"(before[._-]?patch|baseline|repro_run|poc[._-]?(run|output|test)|"
    r"exploit[._-]?validation|exploit[._-]?validator|asan[._-]?before)",
    re.IGNORECASE,
)

VERDICT_PASS_RE = re.compile(r"^\s*VERDICT:\s*PASS\b", re.IGNORECASE | re.MULTILINE)
VERDICT_FAIL_RE = re.compile(r"^\s*VERDICT:\s*FAIL\b", re.IGNORECASE | re.MULTILINE)
RESULT_PASS_RE = re.compile(
    r"(ALL\s+TESTS?\s+PASSED|ALL\s+VALIDATIONS?\s+PASS|"
    r"VALIDATION\s+SUMMARY[^\n]{0,40}?\bPASS\b|"
    r"OVERALL\s*[:=]\s*PASS|FINAL\s+VERDICT[^\n]{0,40}?\bPASS\b|"
    r"RESULT\s*[:=]\s*PASS|\bPATCH\s+VALIDATED\b|"
    r"\bSUCCESS(?:FULLY)?\s+(?:patched|fixed|validated|verified)\b)",
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
    r"vulnerability\s+(?:is\s+)?(?:fixed|patched|resolved|mitigated)|"
    r"all\s+phases\s+completed|both\s+(?:verdicts?|validations?)\s*[:=]?\s*PASS|"
    r"fix\s+(?:verified|validated)|fully\s+fixed|fix\s+is\s+complete|"
    r"patch\s+effectively\s+(?:removes|eliminates))",
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


def collect_b1_runs() -> list[tuple[Path, dict]]:
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
        if new_rank < cur_rank:
            best[task] = (child, m)
        elif new_rank == cur_rank and m["started_at"] > cur[1]["started_at"]:
            best[task] = (child, m)
    return sorted(best.values(), key=lambda x: x[1]["task"])


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
    fail_re = RESULT_FAIL_RE  # same regex for report-fail signal here
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
    for p in sorted(testcase.iterdir()):
        if not p.is_file():
            continue
        if p.stat().st_size > 5 * 1024 * 1024:  # skip giant binaries
            continue
        if p.suffix.lower() in {".bin", ".tiff", ".pgx", ".wav", ".pdf",
                                ".png", ".jpg", ".mp4", ".mat"}:
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
    runs = collect_b1_runs()
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
            "task": m["task"],
            "run_id": run_dir.name,
            "exit_status": m["exit_status"],
            "patch_claim": patch_claim,
            "report_claim": report_claim,
            "patch_v_file": patch_v_path.name if patch_v_path else "",
            "post_patch_san_files": ev["post_patch_san"],
            "pre_patch_san_files": ev["pre_patch_san"],
            "other_san_files": [n for n in ev["all_san"]
                                if n not in ev["post_patch_san"]
                                and n not in ev["pre_patch_san"]],
        })

    print(f"Audited {len(rows)} deduped B1 tasks\n")

    print("Claim x evidence summary:")
    n_post = sum(1 for r in rows if r["post_patch_san_files"])
    n_patch_pass = sum(1 for r in rows if r["patch_claim"] == "PASS")
    n_report_pass = sum(1 for r in rows if r["report_claim"] == "PASS")
    n_fp = sum(1 for r in rows
               if r["post_patch_san_files"]
               and (r["patch_claim"] == "PASS" or r["report_claim"] == "PASS"))
    print(f"  patch_claim=PASS:           {n_patch_pass}")
    print(f"  report_claim=PASS:          {n_report_pass}")
    print(f"  runs with post-patch ASan:  {n_post}")
    print(f"  FP candidates (claim PASS + post-patch ASan): {n_fp}")
    print()

    print("=" * 100)
    print("FP CANDIDATES (patch or report claims PASS, but a post-patch "
          "sanitizer file exists)")
    print("=" * 100)
    for r in sorted(rows, key=lambda x: x["task"]):
        if not r["post_patch_san_files"]:
            continue
        if r["patch_claim"] != "PASS" and r["report_claim"] != "PASS":
            continue
        print(f"\n{r['task']}  ({r['run_id'][:12]}, exit={r['exit_status']})")
        print(f"  patch_claim={r['patch_claim']}  report_claim={r['report_claim']}  "
              f"patch_v_file={r['patch_v_file']!r}")
        print(f"  post-patch ASan files: {r['post_patch_san_files']}")
        if r["pre_patch_san_files"]:
            print(f"  pre-patch ASan files (for context): "
                  f"{r['pre_patch_san_files'][:5]}")

    print()
    print("=" * 100)
    print("Runs claiming PASS with NO sanitizer evidence at all "
          "(no post-patch + no pre-patch logs — likely skipped validation)")
    print("=" * 100)
    for r in sorted(rows, key=lambda x: x["task"]):
        if r["patch_claim"] != "PASS" and r["report_claim"] != "PASS":
            continue
        if r["post_patch_san_files"] or r["pre_patch_san_files"] or r["other_san_files"]:
            continue
        print(f"  {r['task']}  ({r['run_id'][:12]})  patch={r['patch_claim']} "
              f"report={r['report_claim']}")

    out = Path("/tmp/b1_evidence_sweep.json")
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nFull JSON: {out}")


if __name__ == "__main__":
    main()
