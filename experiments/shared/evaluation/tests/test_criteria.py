"""Tests for criteria metrics (artifacts #5 and success #6)."""

from experiments.shared.evaluation.bef import BefRunEvaluator
from experiments.shared.evaluation.tests.builders import RunBuilder, write_files


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
    assert "patch applies cleanly" in fixer["declared_criteria"]
    assert fixer["self_report"]["work_completed"] == ["patch created"]
    assert fixer["self_report"]["verification_passed"] == 1
    # And: an empty subtree reports missing files and empty criteria
    builder_phase = result["Builder"]
    assert builder_phase["key_files_exist"]["/testcase/base_commit_hash"] is False
    assert builder_phase["declared_criteria"] == []
    assert builder_phase["self_report"]["work_completed"] == []


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


def test_success_key_file_glob_and_vacuous(tmp_path) -> None:
    """PoC glob matches any non-empty poc* file; an empty match does not count."""
    # Given: an Exploiter subtree with poc artifacts on disk
    builder = RunBuilder()
    boss = builder.boss()
    builder.agent("worker", boss, "[Exploiter] exploit")
    run_dir = write_files(
        tmp_path, {"/testcase/poc.php": b"<?php crash();", "/testcase/repro.sh": b""}
    )
    # When
    result = BefRunEvaluator(builder.run_data(run_dir)).success_criteria_by_bef()
    # Then
    exploiter = result["Exploiter"]
    assert exploiter["key_files_exist"]["/testcase/poc*"] is True
    assert exploiter["key_files_exist"]["/testcase/repro.sh"] is False  # empty -> vacuous
