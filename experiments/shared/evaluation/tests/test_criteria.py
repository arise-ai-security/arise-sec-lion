"""Tests for criteria metrics (artifacts #5 and success #6)."""

from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.criteria import (
    built_success_mechanical,
    exploited_success_mechanical,
)
from experiments.shared.evaluation.tests.builders import RunBuilder, write_files

_ELF = b"\x7fELF" + b"\x00" * 64


def test_artifacts_by_bef_excludes_vacuous_and_dedups(tmp_path) -> None:
    """Only non-vacuous edited files are listed, once per subtree."""
    # Given: a Fixer worker edits a real patch, an empty log, and the patch again
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Fixer] fix")
    worker = builder.agent("worker", manager, "[Patch-Creator] patch")
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
    worker = builder.agent("worker", manager, "[Patch-Creator] patch")
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
    # Given: a Fixer decomposition where Patch-Creator has its producer sibling
    # present, but omits the required depends_on edge to Root-Cause-Analyst.
    builder = RunBuilder()
    boss = builder.boss()
    manager = builder.agent("manager", boss, "[Fixer] fix")
    builder.agent("worker", manager, "[Root-Cause-Analyst] analyze", sibling_index=0)
    builder.agent("worker", manager, "[Patch-Creator] patch", sibling_index=1)
    run_dir = write_files(tmp_path, {"/testcase/model_patch.diff": b"diff"})

    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).success_criteria_by_bef()

    # Then: the prompt-only dependency contract is reported as failed.
    fixer_contract = result["Fixer"]["dependency_contract"]
    assert fixer_contract["ok"] is False
    assert any("[Patch-Creator]" in item for item in fixer_contract["violations"])


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


def test_built_success_ignores_stale_events_jsonl(tmp_path) -> None:
    """Mechanical criteria must not use events.jsonl instead of DB-loaded events."""
    # Given: filesystem build evidence plus a stale events.jsonl claiming Builder success.
    run_dir = write_files(
        tmp_path,
        {
            "/testcase/build.exit": b"exit=0\n",
            "/testcase/binary_paths.txt": b"/work/bin/prog\n",
            "/events.jsonl": (
                b'{"event_type":"WorkCompleted","result":"builder build success"}\n'
            ),
        },
    )

    # When: no DB-loaded events are supplied.
    result = built_success_mechanical(run_dir)

    # Then: the stale JSONL event is ignored; event evidence remains absent.
    assert result["verdict"] is False


def test_exploited_handoff_unparseable_does_not_pass(tmp_path) -> None:
    """A repro.sh naming no build-product binary must NOT vacuously satisfy handoff."""
    # Given: a non-vacuous repro.sh (past the length gate) that invokes no parseable binary
    repro = b"#!/bin/bash\n# reproduce the reported crash with the proof of concept\necho done\n"
    run_dir = write_files(tmp_path, {"/testcase/repro.sh": repro})
    # When
    result = exploited_success_mechanical(run_dir, events=[])
    # Then: the contract is surfaced as unparseable, not silently passed
    assert result["repro_bin_target"] is None
    assert result["handoff_contract_ok"] is False
    assert result["handoff_unparseable"] is True


def test_exploited_handoff_requires_real_build_product(tmp_path) -> None:
    """repro.sh pointing at a missing binary fails handoff; a real one passes."""
    repro = b"#!/bin/bash\n# run the built binary on the poc\n/work/bin/prog /testcase/poc.bin\n"
    # Given: repro.sh points at /work/bin/prog, which does not exist on disk
    missing_dir = write_files(tmp_path / "missing", {"/testcase/repro.sh": repro})
    missing = exploited_success_mechanical(missing_dir, events=[])
    # Then: parseable but unresolved -> contract not satisfied (and not "unparseable")
    assert missing["repro_bin_target"] == "/work/bin/prog"
    assert missing["handoff_contract_ok"] is False
    assert missing["handoff_unparseable"] is False

    # And: when the Builder product actually exists non-vacuously, handoff holds
    ok_dir = write_files(tmp_path / "ok", {"/testcase/repro.sh": repro, "/work/bin/prog": _ELF})
    ok = exploited_success_mechanical(ok_dir, events=[])
    assert ok["handoff_contract_ok"] is True


def test_exploited_handoff_skips_ld_library_path_dir(tmp_path) -> None:
    """An LD_LIBRARY_PATH .libs DIR must not shadow the real binary on a later line."""
    # Given: a library-dir export, then the actual /.libs/<file> invocation (the real
    # corpus shape that the first-line-wins parser previously mis-extracted as a dir).
    repro = (
        b"#!/bin/bash\n"
        b"export LD_LIBRARY_PATH=/src/proj/libfoo/.libs:$LD_LIBRARY_PATH\n"
        b"/src/proj/frontend/.libs/foo /testcase/poc.bin\n"
    )
    run_dir = write_files(
        tmp_path, {"/testcase/repro.sh": repro, "/src/proj/frontend/.libs/foo": _ELF}
    )
    # When
    result = exploited_success_mechanical(run_dir, events=[])
    # Then: the binary (not the LD_LIBRARY_PATH dir) is parsed and resolves
    assert result["repro_bin_target"] == "/src/proj/frontend/.libs/foo"
    assert result["handoff_contract_ok"] is True


def test_exploited_handoff_parses_relative_binary_after_cd(tmp_path) -> None:
    """The broadened parser prefers the work/bin invocation over a `cd` src dir."""
    # Given: a repro that cd's into the src tree then runs a relative work/bin binary
    repro = b"#!/bin/bash\ncd /src/demo && ./work/bin/prog /testcase/poc.bin\n"
    run_dir = write_files(tmp_path, {"/testcase/repro.sh": repro, "/work/bin/prog": _ELF})
    # When
    result = exploited_success_mechanical(run_dir, events=[])
    # Then: the work/bin path wins over /src/demo and resolves to the on-disk product
    assert result["repro_bin_target"] == "./work/bin/prog"
    assert result["handoff_contract_ok"] is True


def test_exploited_success_uses_selected_poc_pointer_not_poc_glob(tmp_path) -> None:
    """A non-poc* testcase selected through poc_path.txt satisfies PoC evidence."""
    # Given: a complete exploit artifact set where the selected input is not named poc*.
    repro = b"#!/bin/bash\n/work/bin/prog /testcase/trigger.input\n"
    run_dir = write_files(
        tmp_path,
        {
            "/testcase/poc_path.txt": b"/testcase/trigger.input\n",
            "/testcase/trigger.input": b"crash bytes",
            "/testcase/repro.sh": repro,
            "/testcase/exploit_validation_results.txt": (
                b"VERDICT: PASS\n"
                b"OBSERVED_SANITIZER_ERROR: AddressSanitizer heap-buffer-overflow\n"
                b"DETERMINISM_RUNS: 3/3\n"
            ),
            "/testcase/repro_run_1.log": b"AddressSanitizer: error\n",
            "/work/bin/prog": _ELF,
        },
    )

    # When
    result = exploited_success_mechanical(run_dir, events=[])

    # Then: the selected PoC pointer, not a poc* glob, drives PoC evidence.
    assert result["poc_exists"] is True
    assert result["verdict"] is True


def test_exploited_success_rejects_missing_selected_poc(tmp_path) -> None:
    """poc_path.txt itself is not PoC evidence; the selected file must exist."""
    # Given: validation and binary evidence exist, but poc_path.txt points nowhere.
    repro = b"#!/bin/bash\n/work/bin/prog /testcase/missing.input\n"
    run_dir = write_files(
        tmp_path,
        {
            "/testcase/poc_path.txt": b"/testcase/missing.input\n",
            "/testcase/repro.sh": repro,
            "/testcase/exploit_validation_results.txt": (
                b"VERDICT: PASS\n"
                b"OBSERVED_SANITIZER_ERROR: AddressSanitizer heap-buffer-overflow\n"
                b"DETERMINISM_RUNS: 3/3\n"
            ),
            "/testcase/repro_run_1.log": b"AddressSanitizer: error\n",
            "/work/bin/prog": _ELF,
        },
    )

    # When
    result = exploited_success_mechanical(run_dir, events=[])

    # Then: poc_path.txt does not count unless its selected input exists.
    assert result["poc_exists"] is False
    assert result["verdict"] is False


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
