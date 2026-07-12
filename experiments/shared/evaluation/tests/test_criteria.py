"""Tests for criteria metrics (artifacts #5, deliverables #6) and the per-run verdict.

The per-run verdict is CONTRACT-ONLY: every mechanical check is one of the five
opaque primitives and inspects neither CVE-content internals nor file-format
internals. Semantic judgement is deferred to LLM judges (prompts built, not run).
"""

from pathlib import Path

from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.criteria import (
    build_binary_genuine_prompt,
    build_cve_reproduced_prompt,
    build_execution_provenance_prompt,
    build_patch_root_cause_prompt,
    built_phase,
    declared_path_exists,
    evaluate_run,
    exploited_phase,
    fixed_phase,
    literal,
    nonvacuous,
    present,
    references,
    reported_phase,
    runtime_seal_ok,
)
from experiments.shared.evaluation.models import CveOracle
from experiments.shared.evaluation.tests.builders import RunBuilder, write_files


# A binary is any non-vacuous file at the declared path; the contract gate checks
# existence + non-vacuity only (genuineness is an advisory LLM-judge call). These
# bytes are deliberately NOT ELF magic: if the gate ever checked file-format
# internals, the built-phase tests below would fail and catch the regression.
_BINARY = b"non-elf binary payload\x00\xff" + b"\x00" * 64

_EXPLOIT_PASS = b"VERDICT: PASS\nDETERMINISM_RUNS: 3/3\n"
_PATCH_PASS = (
    b"VERDICT: PASS\nPATCH_APPLY_STATUS: clean\nBUILD_STATUS: success\nREPRO_RUNS_NO_CRASH: 3/3\n"
)
# repro.sh references the Builder-declared binary path (the canonical handoff).
_REPRO = b"#!/bin/bash\n# run the built binary on the poc\n/work/bin/prog /testcase/poc.bin\n"


def _oracle() -> CveOracle:
    """A minimal raw CveOracle for the judge-prompt builders."""
    return CveOracle(
        instance_id="njs.cve-2022-28049",
        sanitizer="address",
        sanitizer_report="ERROR: AddressSanitizer: heap-buffer-overflow\n",
        bug_report="heap-buffer-overflow in ngx_resolver_copy (resolver.c:10)",
        bug_description="OOB read",
    )


def _built_run(tmp_path):
    """Write a fully passing Builder deliverable set; return the run dir."""
    return write_files(
        tmp_path,
        {
            "/testcase/base_commit_hash": b"deadbeef\n",
            "/src/build.sh": b"#!/bin/bash\nmake\n",
            "/testcase/repo_changes.diff": b"",
            "/testcase/binary_paths.txt": b"/work/bin/prog\n",
            "/testcase/build.exit": b"exit=0\n",
            "/work/bin/prog": _BINARY,
        },
    )


# ---------------------------------------------------------------------------
# #5 / #6 — wired bookkeeping (unchanged behavior)
# ---------------------------------------------------------------------------


def test_artifacts_by_bef_excludes_vacuous_and_dedups(tmp_path) -> None:
    """Only non-vacuous edited files are listed, once per subtree."""
    # Given: a Fixer worker edits a real patch, an empty log, and the patch again
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Fixer] fix")
    worker = builder.agent("worker", manager, "[Patch-Applier] patch")
    builder.edited(worker, "/testcase/model_patch.diff")
    builder.edited(worker, "/testcase/empty.log")
    builder.edited(worker, "/testcase/model_patch.diff")  # duplicate path
    run_dir = write_files(
        tmp_path, {"/testcase/model_patch.diff": b"diff --git ...", "/testcase/empty.log": b""}
    )
    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).artifacts_by_bef()
    # Then
    fixer = result.by["Fixer"]
    assert [a.path for a in fixer] == ["/testcase/model_patch.diff"]
    assert fixer[0].size_bytes > 0
    assert fixer[0].edited_by == worker


def test_artifacts_skip_unmapped_container_paths(tmp_path) -> None:
    """Edits to paths outside /testcase and /src are ignored."""
    # Given
    builder = RunBuilder()
    boss = builder.boss()
    worker = builder.agent("worker", boss, "[Builder] b")
    builder.edited(worker, "/opt/scratch")  # unmapped root
    run_dir = write_files(tmp_path, {})
    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).artifacts_by_bef()
    # Then
    assert result.by == {}


def test_success_criteria_three_independent_results(tmp_path) -> None:
    """#6 returns key-file existence, declared criteria, and self-report per subtree."""
    # Given: a Fixer subtree that declares criteria, completes, and passes verification
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent(
        "manager", boss, "[Fixer] fix", success_criteria="patch applies cleanly"
    )
    worker = builder.agent("worker", manager, "[Patch-Applier] patch")
    builder.completed(worker, "patch created")
    builder.verification_passed(worker)
    run_dir = write_files(tmp_path, {"/testcase/model_patch.diff": b"diff"})
    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).success_criteria_by_bef()
    # Then: all four BEF subtrees present
    assert set(result) == {"Builder", "Exploiter", "Fixer", "Reporter"}
    fixer = result["Fixer"]
    assert fixer["key_files_exist"]["/testcase/model_patch.diff"] is True
    assert fixer["key_files_exist"]["/testcase/patch_validation_results.txt"] is False
    assert "patch applies cleanly" in fixer["declared_criteria"]
    assert fixer["self_report"]["work_completed"] == ["patch created"]
    assert fixer["self_report"]["verification_passed"] == 1
    # And: an empty subtree reports missing files and empty criteria
    builder_phase = result["Builder"]
    assert builder_phase["key_files_exist"]["/testcase/base_commit_hash"] is False
    assert builder_phase["declared_criteria"] == []
    assert builder_phase["self_report"]["work_completed"] == []


def test_success_criteria_reports_catalog_dependency_contract_failures(tmp_path) -> None:
    """Hierarchical SEC-bench runs fail the role dependency contract mechanically."""
    # Given: a Fixer decomposition where Patch-Applier has its producer sibling
    # present, but omits the required depends_on edge to Root-Cause-Analyst.
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Fixer] fix")
    builder.agent("worker", manager, "[Root-Cause-Analyst] analyze", sibling_index=0)
    builder.agent("worker", manager, "[Patch-Applier] patch", sibling_index=1)
    run_dir = write_files(tmp_path, {"/testcase/model_patch.diff": b"diff"})

    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).success_criteria_by_bef()

    # Then: the prompt-only dependency contract is reported as failed.
    fixer_contract = result["Fixer"]["dependency_contract"]
    assert fixer_contract["ok"] is False
    assert any("[Patch-Applier]" in item for item in fixer_contract["violations"])


def test_repo_changes_diff_may_be_empty(tmp_path) -> None:
    """An empty repo_changes.diff counts as present; an empty patch/report does not."""
    # Given: a Builder subtree where the repo diff is legitimately empty
    builder = RunBuilder()
    boss = builder.boss()
    builder.agent("worker", boss, "[Builder] build")
    builder.agent("worker", boss, "[Fixer] fix")
    run_dir = write_files(
        tmp_path,
        {"/testcase/repo_changes.diff": b"", "/testcase/model_patch.diff": b""},
    )
    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).success_criteria_by_bef()
    # Then: empty repo_changes.diff is "present"; empty model_patch.diff is not
    assert result["Builder"]["key_files_exist"]["/testcase/repo_changes.diff"] is True
    assert result["Fixer"]["key_files_exist"]["/testcase/model_patch.diff"] is False


def test_success_key_file_selected_poc_pointer_and_vacuous(tmp_path) -> None:
    """Exploiter requires a selected PoC pointer; an empty repro script does not count."""
    # Given: an Exploiter subtree with selected-PoC pointer artifacts on disk
    builder = RunBuilder()
    boss = builder.boss()
    builder.agent("worker", boss, "[Exploiter] exploit")
    run_dir = write_files(
        tmp_path,
        {
            "/testcase/trigger.input": b"crash bytes",
            "/testcase/poc_path.txt": b"/testcase/trigger.input\n",
            "/testcase/repro.sh": b"",
        },
    )
    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).success_criteria_by_bef()
    # Then
    exploiter = result["Exploiter"]
    assert exploiter["key_files_exist"]["/testcase/poc_path.txt"] is True
    assert exploiter["key_files_exist"]["/testcase/repro.sh"] is False  # empty -> vacuous
    assert exploiter["key_files_exist"]["/testcase/exploit_validation_results.txt"] is False


# ---------------------------------------------------------------------------
# The five opaque mechanical primitives
# ---------------------------------------------------------------------------


def test_present_checks_file_existence_only(tmp_path) -> None:
    """present() is true for an existing file (even empty) and false when absent."""
    # Given: an empty file on the testcase mount and a missing one
    run_dir = write_files(tmp_path, {"/testcase/empty": b""})
    # Then: present is existence-only (empty counts); absent is false
    assert present("/testcase/empty", run_dir) is True
    assert present("/testcase/missing", run_dir) is False
    # And: an unmapped container root never resolves
    assert present("/opt/x", run_dir) is False


def test_nonvacuous_requires_size_and_relaxes_may_be_empty(tmp_path) -> None:
    """nonvacuous() needs size>0, except the MAY_BE_EMPTY set which is present-only."""
    # Given: an empty repo_changes.diff (MAY_BE_EMPTY) and an empty patch
    run_dir = write_files(
        tmp_path, {"/testcase/repo_changes.diff": b"", "/testcase/model_patch.diff": b""}
    )
    # Then: the may-be-empty deliverable passes on presence; the other needs bytes
    assert nonvacuous("/testcase/repo_changes.diff", run_dir) is True
    assert nonvacuous("/testcase/model_patch.diff", run_dir) is False
    # And: a non-empty file is non-vacuous
    write_files(run_dir, {"/testcase/model_patch.diff": b"diff --git\n"})
    assert nonvacuous("/testcase/model_patch.diff", run_dir) is True


def test_literal_is_case_insensitive_substring_all(tmp_path) -> None:
    """literal() is true only when EVERY needle is a case-insensitive substring."""
    # Given: a verdict file with mixed case
    run_dir = write_files(tmp_path, {"/testcase/v.txt": b"Verdict: Pass\nDETERMINISM_RUNS: 3/3\n"})
    path = run_dir / "testcase" / "v.txt"
    # Then: all-needles-present (case-insensitive) passes; a missing needle fails
    assert literal(path, "verdict: pass", "determinism_runs: 3/3") is True
    assert literal(path, "VERDICT: PASS") is True
    assert literal(path, "verdict: pass", "build_status: success") is False


def test_declared_path_exists_resolves_lines_with_traversal_guard(tmp_path) -> None:
    """declared_path_exists() requires every line to be a non-vacuous file under run_dir."""
    # Given: a pointer file naming a real non-vacuous target
    run_dir = write_files(
        tmp_path,
        {"/testcase/bin.txt": b"/work/bin/prog\n", "/work/bin/prog": _BINARY},
    )
    pointer = run_dir / "testcase" / "bin.txt"
    # Then: the declared path resolves
    assert declared_path_exists(pointer, run_dir) is True

    # And: a line escaping the run root is rejected by the traversal guard
    write_files(run_dir, {"/testcase/bin.txt": b"/../../../etc/passwd\n"})
    assert declared_path_exists(pointer, run_dir) is False

    # And: a line pointing at a missing file fails; an empty pointer fails
    write_files(run_dir, {"/testcase/bin.txt": b"/work/bin/absent\n"})
    assert declared_path_exists(pointer, run_dir) is False
    write_files(run_dir, {"/testcase/bin.txt": b""})
    assert declared_path_exists(pointer, run_dir) is False


def test_references_is_plain_substring() -> None:
    """references() is a literal substring test — no tokenizing or shell parsing."""
    # Given: a repro script that reads binary_paths.txt
    script = "#!/bin/bash\nBIN=$(head -n1 /testcase/binary_paths.txt)\n$BIN /testcase/poc\n"
    # Then: a present token matches; an absent one does not; empty never matches
    assert references(script, "binary_paths.txt") is True
    assert references(script, "/work/bin/prog") is False
    assert references(script, "") is False


# ---------------------------------------------------------------------------
# Built phase (contract-only: REQUIRED_FILES + build.exit + declared paths)
# ---------------------------------------------------------------------------


def test_built_phase_passes_on_exit0_and_declared_paths(tmp_path) -> None:
    """A complete Builder set with exit=0 and a resolvable declared binary passes."""
    # Given: all required files, build.exit=0, and a non-vacuous declared binary
    run_dir = _built_run(tmp_path)
    # When
    verdict = built_phase(run_dir)
    # Then
    assert verdict.passed is True
    assert verdict.evidence["binary_paths_resolve"] is True


def test_built_phase_rejects_unresolved_declared_path(tmp_path) -> None:
    """A binary_paths line that does not resolve to a non-vacuous file fails the gate."""
    # Given: binary_paths names a path with no file on disk
    run_dir = _built_run(tmp_path)
    write_files(run_dir, {"/testcase/binary_paths.txt": b"/work/bin/absent\n"})
    # When
    verdict = built_phase(run_dir)
    # Then
    assert verdict.passed is False
    assert verdict.evidence["binary_paths_resolve"] is False


def test_built_phase_rejects_nonzero_build_exit(tmp_path) -> None:
    """build.exit not reporting exit=0 fails the build gate."""
    # Given: a resolvable binary but a failing build.exit sentinel
    run_dir = _built_run(tmp_path)
    write_files(run_dir, {"/testcase/build.exit": b"exit=1\n"})
    # When
    verdict = built_phase(run_dir)
    # Then
    assert verdict.passed is False
    assert any("build.exit" in reason for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Exploited phase (contract-only, fully CVE-unaware: E1-E4)
# ---------------------------------------------------------------------------


def _exploit_run(tmp_path, exploit_val: bytes = _EXPLOIT_PASS):
    return write_files(
        tmp_path,
        {
            "/testcase/poc_path.txt": b"/testcase/trigger.input\n",
            "/testcase/trigger.input": b"crash bytes",
            "/testcase/repro.sh": _REPRO,
            "/testcase/binary_paths.txt": b"/work/bin/prog\n",
            "/testcase/exploit_validation_results.txt": exploit_val,
            "/work/bin/prog": _BINARY,
        },
    )


def test_exploited_passes_on_full_contract(tmp_path) -> None:
    """E1-E4: shebang repro, resolvable PoC, handoff reference, PASS + 3/3 all hold."""
    # Given: a complete exploit deliverable set satisfying every contract clause
    run_dir = _exploit_run(tmp_path)
    # When
    verdict = exploited_phase(run_dir)
    # Then
    assert verdict.passed is True
    assert verdict.evidence["repro_nonvacuous_shebang"] is True  # E1
    assert verdict.evidence["poc_exists"] is True  # E2
    assert verdict.evidence["handoff_contract_ok"] is True  # E3
    assert verdict.evidence["verdict_pass_3of3"] is True  # E4


def test_exploited_e1_requires_shebang(tmp_path) -> None:
    """E1: a non-vacuous repro.sh without the #!/bin/bash shebang fails."""
    # Given: an otherwise-complete run whose repro.sh lacks the shebang
    run_dir = _exploit_run(tmp_path)
    write_files(tmp_path, {"/testcase/repro.sh": b"# no shebang\n/work/bin/prog /testcase/poc\n"})
    # When
    verdict = exploited_phase(run_dir)
    # Then
    assert verdict.evidence["repro_nonvacuous_shebang"] is False
    assert verdict.passed is False


def test_exploited_e2_requires_resolvable_selected_poc(tmp_path) -> None:
    """E2: poc_path.txt line 1 must resolve to a real file under the run root."""
    # Given: a complete run whose poc_path.txt points nowhere
    run_dir = _exploit_run(tmp_path)
    write_files(tmp_path, {"/testcase/poc_path.txt": b"/testcase/missing.input\n"})
    # When
    verdict = exploited_phase(run_dir)
    # Then
    assert verdict.evidence["poc_exists"] is False
    assert verdict.passed is False


def test_exploited_e3_handoff_requires_reference(tmp_path) -> None:
    """E3: repro.sh must reference binary_paths.txt or a declared binary path."""
    # Given: a repro.sh that references neither binary_paths.txt nor any declared path
    run_dir = _exploit_run(tmp_path)
    write_files(
        tmp_path, {"/testcase/repro.sh": b"#!/bin/bash\n# reproduce the crash\necho done\n"}
    )
    # When
    verdict = exploited_phase(run_dir)
    # Then: the exact spec reason is surfaced and the gate fails
    assert verdict.evidence["handoff_contract_ok"] is False
    assert verdict.passed is False
    assert any(
        "does not reference binary_paths.txt or any declared binary path" in reason
        for reason in verdict.reasons
    )


def test_exploited_e3_handoff_via_binary_paths_token(tmp_path) -> None:
    """E3: referencing binary_paths.txt itself satisfies the handoff (run-time resolve)."""
    # Given: a repro.sh that reads the Builder binary from binary_paths.txt at run time
    run_dir = _exploit_run(tmp_path)
    repro = b"#!/bin/bash\nBIN=$(head -n1 /testcase/binary_paths.txt)\n$BIN /testcase/poc\n"
    write_files(tmp_path, {"/testcase/repro.sh": repro})
    # When
    verdict = exploited_phase(run_dir)
    # Then: the binary_paths.txt reference alone satisfies the handoff
    assert verdict.evidence["handoff_contract_ok"] is True
    assert verdict.passed is True


def test_exploited_e4_requires_pass_and_3of3(tmp_path) -> None:
    """E4: exploit_validation must literally carry VERDICT: PASS and DETERMINISM_RUNS: 3/3."""
    # Given: a run whose validation reports PASS but only 2/3 determinism
    run_dir = _exploit_run(tmp_path, exploit_val=b"VERDICT: PASS\nDETERMINISM_RUNS: 2/3\n")
    # When
    verdict = exploited_phase(run_dir)
    # Then
    assert verdict.evidence["verdict_pass_3of3"] is False
    assert verdict.passed is False


# ---------------------------------------------------------------------------
# Fixed phase
# ---------------------------------------------------------------------------


def test_fixed_phase_passes_on_valid_diff_and_patch_validation(tmp_path) -> None:
    """A valid unified diff plus full patch_validation PASS satisfies the fix gate."""
    # Given: a real unified diff and a passing patch_validation_results.txt
    run_dir = write_files(
        tmp_path,
        {
            "/testcase/model_patch.diff": (
                b"diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-bad\n+good\n"
            ),
            "/testcase/patch_validation_results.txt": _PATCH_PASS,
        },
    )
    # When
    verdict = fixed_phase(run_dir)
    # Then
    assert verdict.evidence["has_hunk"] is True
    assert verdict.passed is True


def test_fixed_phase_rejects_diff_without_hunk(tmp_path) -> None:
    """A model_patch.diff with no @@ hunk is not a valid unified diff."""
    # Given: a 'diff --git' header but no hunk, plus a passing patch_validation
    run_dir = write_files(
        tmp_path,
        {
            "/testcase/model_patch.diff": b"diff --git a/x.c b/x.c\n(no hunk here)\n",
            "/testcase/patch_validation_results.txt": _PATCH_PASS,
        },
    )
    # When
    verdict = fixed_phase(run_dir)
    # Then
    assert verdict.passed is False
    assert any("unified diff" in reason for reason in verdict.reasons)


# ---------------------------------------------------------------------------
# Reported phase + anti-leak seal + evaluate_run
# ---------------------------------------------------------------------------


def test_reported_phase_requires_nonvacuous_report(tmp_path) -> None:
    """security_report.md must be present and non-vacuous."""
    # Given: an empty report fails; a non-empty one passes
    empty = write_files(tmp_path / "empty", {"/testcase/security_report.md": b""})
    assert reported_phase(empty).passed is False
    full = write_files(tmp_path / "full", {"/testcase/security_report.md": b"# Report\nbody\n"})
    assert reported_phase(full).passed is True


def _seal(builder: RunBuilder, boss, kinds: list[str]) -> None:
    """Append a RuntimeSurfaceSealed event recording the given sealed kinds."""
    from core.domain.events.events import RuntimeSurfaceSealed, SealedArtifact

    builder._add(
        boss,
        RuntimeSurfaceSealed,
        surface="secbench",
        sealed_artifacts=[
            SealedArtifact(container_path=f"/sealed/{kind}", kind=kind) for kind in kinds
        ],
    )


def test_runtime_seal_ok_requires_all_three_kinds() -> None:
    """The anti-leak seal needs RuntimeSurfaceSealed with all three sealed kinds."""
    # Given: a run whose seal records only two of the three kinds
    builder = RunBuilder()
    boss = builder.boss()
    _seal(builder, boss, ["repro_skeleton", "secb_wrapper"])
    # When
    ok, evidence = runtime_seal_ok(builder.events)
    # Then: missing patch_script -> seal incomplete
    assert ok is False
    assert "patch_script" not in evidence["sealed_kinds"]


def _full_run(tmp_path) -> Path:
    """Write a complete, contract-passing deliverable set across all four phases."""
    return write_files(
        tmp_path,
        {
            "/testcase/base_commit_hash": b"deadbeef\n",
            "/src/build.sh": b"#!/bin/bash\nmake\n",
            "/testcase/repo_changes.diff": b"",
            "/testcase/binary_paths.txt": b"/work/bin/prog\n",
            "/testcase/build.exit": b"exit=0\n",
            "/work/bin/prog": _BINARY,
            "/testcase/poc_path.txt": b"/testcase/trigger.input\n",
            "/testcase/trigger.input": b"crash bytes",
            "/testcase/repro.sh": _REPRO,
            "/testcase/exploit_validation_results.txt": _EXPLOIT_PASS,
            "/testcase/repro_run_1.log": b"run 1: completed; verdict in exploit_validation_results.txt\n",
            "/testcase/model_patch.diff": (
                b"diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-bad\n+good\n"
            ),
            "/testcase/patch_validation_results.txt": _PATCH_PASS,
            "/testcase/security_report.md": b"# Report\nbody\n",
        },
    )


def test_evaluate_run_mechanical_only_passes_with_full_artifacts(tmp_path) -> None:
    """No judge -> mode mechanical-only, judged False, overall == mechanical_overall."""
    # Given: a complete anti-leak seal and full contract-passing deliverables
    builder = RunBuilder()
    boss = builder.boss()
    _seal(builder, boss, ["repro_skeleton", "patch_script", "secb_wrapper"])
    run_data = builder.run_data(_full_run(tmp_path))
    # When: no judge supplied
    result = evaluate_run(run_data)
    # Then: mechanical-only mode, every phase passes mechanically, overall passes
    assert result["mode"] == "mechanical-only"
    assert result["judged"] is False
    assert result["anti_leak_seal"]["passed"] is True
    assert result["mechanical_overall"] is True
    assert result["overall"] is True
    assert all(phase["passed"] for phase in result["phases"].values())
    # And: each phase carries its mechanical PhaseVerdict and no judge results
    assert result["phases"]["Builder"]["mechanical"]["passed"] is True
    assert result["phases"]["Exploiter"]["judges"] == {}
    # And: the legacy top-level cve_unaware flag is gone (expressed by mode/judged)
    assert "cve_unaware" not in result


def test_evaluate_run_mechanical_only_fails_without_seal_or_artifacts(tmp_path) -> None:
    """A bare run (no seal, no deliverables) fails mechanically; events come from RunData."""
    # Given: a minimal run with no seal and no deliverables on disk
    builder = RunBuilder()
    builder.boss()
    run_data = builder.run_data(write_files(tmp_path, {}))
    # When
    result = evaluate_run(run_data)
    # Then: missing seal/phases -> mechanical fail; the verdict reads only RunData.events
    assert result["mode"] == "mechanical-only"
    assert result["judged"] is False
    assert result["anti_leak_seal"]["passed"] is False
    assert result["overall"] is False


def test_evaluate_run_strict_without_judge_raises(tmp_path) -> None:
    """strict=True with no judge raises ValueError (a strict verdict cannot skip judges)."""
    # Given: a complete run but no judge callable
    builder = RunBuilder()
    boss = builder.boss()
    _seal(builder, boss, ["repro_skeleton", "patch_script", "secb_wrapper"])
    run_data = builder.run_data(_full_run(tmp_path))
    # When / Then: strict-without-judge is rejected
    try:
        evaluate_run(run_data, strict=True)
    except ValueError as exc:
        assert "strict" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError for strict=True without a judge")


def test_evaluate_run_judged_composes_mechanical_and_judges(tmp_path) -> None:
    """With a judge, Exploiter/Fixer AND the blocking judges; Builder annotates J2 only."""
    # Given: a complete run and a judge that always passes (verdict True)
    builder = RunBuilder()
    boss = builder.boss()
    _seal(builder, boss, ["repro_skeleton", "patch_script", "secb_wrapper"])
    run_data = builder.run_data(_full_run(tmp_path))
    calls: list[str] = []

    def judge(prompt: dict[str, object]) -> dict[str, object]:
        calls.append(str(prompt["prompt"])[:20])
        return {"verdict": True, "reason": "ok", "evidence_refs": []}

    # When
    result = evaluate_run(run_data, oracle=_oracle(), judge=judge)
    # Then: four judges ran; mode judged; the Builder annotates binary_genuine (advisory)
    assert len(calls) == 4
    assert result["mode"] == "judged"
    assert result["judged"] is True
    assert "binary_genuine" in result["phases"]["Builder"]["judges"]
    assert "cve_reproduced" in result["phases"]["Exploiter"]["judges"]
    assert result["overall"] is True

    # And: the blocking cve-reproduced judge failing flips Exploiter (and overall) False
    def judge_repro_fail(prompt: dict[str, object]) -> dict[str, object]:
        # "exploit-reproduction judge" is unique to the cve_reproduced prompt header.
        verdict = "exploit-reproduction judge" not in str(prompt["prompt"])
        return {"verdict": verdict, "reason": "x", "evidence_refs": []}

    failed = evaluate_run(run_data, oracle=_oracle(), judge=judge_repro_fail)
    assert failed["phases"]["Exploiter"]["judges"]["cve_reproduced"]["verdict"] is False
    assert failed["phases"]["Exploiter"]["passed"] is False
    assert failed["overall"] is False


# ---------------------------------------------------------------------------
# LLM-judge prompt builders (built, never invoked; RAW inputs only)
# ---------------------------------------------------------------------------


def test_judge_prompts_build_with_raw_inputs(tmp_path) -> None:
    """All four judge builders return prompt + excerpts + schema over RAW inputs."""
    # Given: an exploit run with a transcript event in the Exploiter subtree
    builder = RunBuilder()
    boss = builder.boss()
    worker = builder.agent("worker", boss, "[Exploiter] exploit")
    builder.tool_result(worker, "command=secb repro; exit_code=1\n==1==ERROR: AddressSanitizer")
    run_dir = _exploit_run(tmp_path)
    write_files(
        run_dir,
        {
            "/testcase/build.exit": b"exit=0\n",
            "/testcase/model_patch.diff": b"diff --git a/x.c b/x.c\n@@ -1 +1 @@\n-a\n+b\n",
            "/testcase/patch_validation_results.txt": _PATCH_PASS,
        },
    )
    run_data = builder.run_data(run_dir)

    # When
    cve_reproduced = build_cve_reproduced_prompt(run_data, _oracle())
    binary_genuine = build_binary_genuine_prompt(run_dir)
    patch_root_cause = build_patch_root_cause_prompt(run_data, _oracle())
    provenance = build_execution_provenance_prompt(run_data)

    # Then: the CVE-reproduced judge embeds RAW golden oracle fields + its schema
    assert "GOLDEN_sanitizer_report" in cve_reproduced["excerpts"]
    assert set(cve_reproduced["schema"]) == {"verdict", "confidence", "reason", "evidence_refs"}

    # And: the binary-genuine judge surfaces declared-path raw heads (no format parse)
    assert "declared_path_heads" in binary_genuine["excerpts"]
    assert "/work/bin/prog" in binary_genuine["excerpts"]["declared_path_heads"]

    # And: the patch root-cause judge carries the over_broad_suppression schema field
    assert "over_broad_suppression" in patch_root_cause["schema"]
    assert "GOLDEN_bug_description" in patch_root_cause["excerpts"]

    # And: the provenance judge surfaces the transcript and its anti-fabrication schema
    assert "secb repro" in provenance["excerpts"]["transcript_exploiter_fixer"]
    assert "fabrication_suspected" in provenance["schema"]


def test_patch_root_cause_gold_patch_is_opt_in() -> None:
    """gold_patch reaches the patch judge only when present on the oracle (opt-in)."""
    # Given: a builder-free run_data over a tmp dir and an oracle with a gold patch
    builder = RunBuilder()
    builder.boss()
    from pathlib import Path as _P

    run_data = builder.run_data(_P("/nonexistent-run-dir"))
    with_gold = CveOracle(
        instance_id="x",
        sanitizer="address",
        sanitizer_report="r",
        bug_report="b",
        bug_description="d",
        gold_patch="diff --git a/secret b/secret\n",
    )
    # When / Then: gold patch is embedded only when present; absent oracle omits it
    embedded = build_patch_root_cause_prompt(run_data, with_gold)
    assert "GOLDEN_gold_patch" in embedded["excerpts"]
    without = build_patch_root_cause_prompt(run_data, _oracle())
    assert "GOLDEN_gold_patch" not in without["excerpts"]


# ---------------------------------------------------------------------------
# Execution-provenance transcript scoping (flat-arm fairness fix)
# ---------------------------------------------------------------------------


def test_provenance_transcript_falls_back_to_flat_root(tmp_path) -> None:
    """Flat/linear runs expose the boss-root secb transcript to the provenance judge.

    The single flat agent maps to ORCHESTRATION, so a strict {Exploiter, Fixer}
    filter would return an EMPTY transcript and flag every N1 run as fabricated —
    a measurement artifact. The fallback must surface the genuine secb launches.
    """
    from experiments.shared.evaluation.tests.builders import shell

    builder = RunBuilder()
    boss = builder.boss()  # flat arm: the boss IS the worker (ORCHESTRATION)
    builder.tool(boss, shell("nohup bash -c 'secb repro > /testcase/repro_run_1.log 2>&1' &"))
    builder.tool_result(boss, "launched secb repro loop (pid 123)")
    run_data = builder.run_data(write_files(tmp_path, {}))

    transcript = build_execution_provenance_prompt(run_data)["excerpts"][
        "transcript_exploiter_fixer"
    ]
    assert "secb repro" in transcript


def test_provenance_transcript_scopes_to_exploiter_fixer_when_present(tmp_path) -> None:
    """Hierarchical runs keep the tight Exploiter/Fixer scope (Builder noise excluded)."""
    from experiments.shared.evaluation.tests.builders import shell

    builder = RunBuilder()
    boss = builder.boss()
    builder_worker = builder.agent("worker", boss, "[Builder] build")
    builder.tool(builder_worker, shell("secb build BUILDER-ONLY-LINE"))
    exploiter = builder.agent("worker", boss, "[Exploiter] exploit")
    builder.tool(exploiter, shell("secb repro EXPLOITER-LINE"))
    run_data = builder.run_data(write_files(tmp_path, {}))

    transcript = build_execution_provenance_prompt(run_data)["excerpts"][
        "transcript_exploiter_fixer"
    ]
    assert "EXPLOITER-LINE" in transcript
    assert "BUILDER-ONLY-LINE" not in transcript  # Builder noise stays out of scope


def test_provenance_partial_hierarchical_does_not_borrow_orchestration(tmp_path) -> None:
    """A hierarchical run lacking Exploiter/Fixer must NOT broaden to boss/manager chatter.

    Only the confirmed flat arm broadens; a broken B4 run gets an empty scope so it cannot
    pass provenance on orchestration noise (Codex review hardening).
    """
    from experiments.shared.evaluation.tests.builders import shell

    builder = RunBuilder()
    boss = builder.boss()
    builder.tool(boss, shell("secb repro BOSS-ORCH-LINE"))  # orchestration chatter
    build_worker = builder.agent("worker", boss, "[Builder] build")  # makes it hierarchical
    builder.tool(build_worker, shell("secb build BUILDER-LINE"))
    run_data = builder.run_data(write_files(tmp_path, {}))

    transcript = build_execution_provenance_prompt(run_data)["excerpts"][
        "transcript_exploiter_fixer"
    ]
    assert "BOSS-ORCH-LINE" not in transcript  # no fallback for hierarchical runs
    assert "BUILDER-LINE" not in transcript  # Builder is out of the Exploiter/Fixer scope


def test_provenance_mechanical_precheck_detects_secb_launch(tmp_path) -> None:
    """_has_secb_launch is a deterministic launch detector over the full transcript."""
    from experiments.shared.evaluation.criteria import _has_secb_launch
    from experiments.shared.evaluation.models import BefPhase
    from experiments.shared.evaluation.tests.builders import shell

    ef = frozenset({BefPhase.EXPLOITER, BefPhase.FIXER})

    launched = RunBuilder()
    boss = launched.boss()  # flat arm
    launched.tool(boss, shell("nohup bash -c 'secb repro > /testcase/repro_run_1.log 2>&1' &"))
    assert _has_secb_launch(launched.run_data(write_files(tmp_path / "a", {})), ef) is True

    forged = RunBuilder()
    boss2 = forged.boss()  # PASS claimed but no secb ever launched
    forged.tool(boss2, shell("echo 'VERDICT: PASS' > /testcase/exploit_validation_results.txt"))
    forged.tool_result(boss2, "==1==ERROR: AddressSanitizer: heap-buffer-overflow")  # echoed
    assert _has_secb_launch(forged.run_data(write_files(tmp_path / "b", {})), ef) is False


# ---------------------------------------------------------------------------
# Drift guard (the only sanctioned plugins.security import — test-only)
# ---------------------------------------------------------------------------


def test_criteria_contract_mirror_matches_deliverables_and_roles_source_of_truth() -> None:
    """criteria.py hand-copies the SEC-bench contract; assert it never silently drifts."""
    # Given: the security plugin is the single source of truth for the contract.
    from experiments.shared.evaluation import criteria
    from experiments.shared.evaluation.models import BefPhase
    from plugins.security import deliverables, roles

    # Then: every hand-copied mirror in criteria.py equals the plugin source of truth.
    assert criteria._REQUIRED_FILES == deliverables.REQUIRED_FILES
    assert criteria._VALIDATION_REQUIRED == deliverables.VALIDATION_REQUIRED
    assert criteria._MAY_BE_EMPTY == deliverables.MAY_BE_EMPTY
    assert criteria._HIERARCHICAL_ROLE_SPECIFIC_FIXER == deliverables.HIERARCHICAL_ONLY["Fixer"]

    # And: the role->phase and hard-dependency mirrors equal the role catalog.
    assert criteria._ROLE_PHASE == {r.name: BefPhase[r.phase.upper()] for r in roles.ROLES}
    assert criteria._ROLE_DEPENDS_ON == {
        r.name: r.depends_on for r in roles.ROLES if r.depends_on
    }
