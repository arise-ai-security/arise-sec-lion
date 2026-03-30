"""Tests for decomposition quality validation (concreteness + completeness)."""

import pytest

from core.domain.services.subtask_parser import (
    parse_assessment_response,
    score_concreteness,
    validate_decomposition_quality,
)
from core.domain.values.subtask import Subtask


class TestScoreConcreteness:
    def test_concrete_task_scores_high(self):
        desc = "Write /testcase/base_commit_hash with the correct commit hash. Run /src/build.sh and verify it builds successfully."
        score = score_concreteness(desc)
        assert score >= 0.6

    def test_abstract_task_scores_low(self):
        desc = "Analyze the vulnerability"
        score = score_concreteness(desc)
        assert score < 0.4

    def test_investigate_without_target_scores_low(self):
        desc = "Investigate potential issues in the codebase"
        score = score_concreteness(desc)
        assert score < 0.4

    def test_analyze_with_deliverable_scores_ok(self):
        """'Analyze' + concrete deliverable should NOT be penalized."""
        desc = "Analyze root cause from sanitizer stack trace and create /testcase/model_patch.diff"
        score = score_concreteness(desc)
        assert score >= 0.4

    def test_short_vague_description_scores_low(self):
        desc = "Fix the bug"
        score = score_concreteness(desc)
        assert score < 0.4

    def test_command_reference_boosts_score(self):
        desc = "Run `git diff --no-color HEAD > /testcase/repo_changes.diff` and verify the file exists"
        score = score_concreteness(desc)
        assert score >= 0.5

    def test_empty_string_scores_zero(self):
        score = score_concreteness("")
        assert score == 0.0

    def test_path_reference_boosts_score(self):
        desc = "Save the commit hash to /testcase/base_commit_hash"
        score = score_concreteness(desc)
        assert score >= 0.4

    def test_success_criteria_boosts_score(self):
        desc = "Apply the patch. The sanitizer error must not appear when running repro.sh"
        score = score_concreteness(desc)
        assert score >= 0.4


def _make_subtask(desc: str) -> Subtask:
    return Subtask(
        description=desc,
        config={
            "strategy": "heuristic",
            "base": {"model": "gpt-4o-mini", "temperature": 0.5, "max_tokens": 4000},
            "tool": "openhands",
        },
    )


class TestValidateDecompositionQuality:
    def test_concrete_subtasks_pass(self):
        parent = "[Builder] Set up build environment and produce sanitizer-enabled build"
        subtasks = [
            _make_subtask(
                "Set up the builder environment, determine base commit from bug description, "
                "save to /testcase/base_commit_hash. Run /src/build.sh to produce a sanitizer-enabled build."
            ),
            _make_subtask(
                "Fix remaining build errors. Record git diff to /testcase/repo_changes.diff. "
                "Verify all deliverables exist."
            ),
        ]
        validate_decomposition_quality(subtasks, parent)  # Should not raise

    def test_abstract_subtask_rejected(self):
        parent = "[Builder] Set up build environment"
        subtasks = [
            _make_subtask("Analyze the codebase"),
            _make_subtask("Fix remaining build errors and save to /testcase/repo_changes.diff"),
        ]
        with pytest.raises(ValueError, match="too abstract"):
            validate_decomposition_quality(subtasks, parent)

    def test_empty_parent_skips_completeness_check(self):
        subtasks = [
            _make_subtask("Write /testcase/base_commit_hash with commit hash abc123"),
        ]
        validate_decomposition_quality(subtasks, "")  # Should not raise (completeness skipped)

    def test_single_subtask_allowed(self):
        parent = "Build the project"
        subtasks = [
            _make_subtask("Run /src/build.sh to compile the project with sanitizer flags enabled"),
        ]
        validate_decomposition_quality(subtasks, parent)


class TestParseAssessmentWithQuality:
    def test_decompose_with_abstract_subtask_raises(self):
        raw = (
            '{"action": "decompose", "reasoning": "Needs split", '
            '"subtasks": [{"description": "Investigate the issue", '
            '"config": {"strategy": "heuristic", "base": {"model": "gpt-4o-mini", '
            '"temperature": 0.5, "max_tokens": 4000}, "tool": "openhands"}}]}'
        )
        with pytest.raises(ValueError, match="too abstract"):
            parse_assessment_response(raw, parent_task="Fix the vulnerability in njs")

    def test_decompose_with_concrete_subtasks_passes(self):
        raw = (
            '{"action": "decompose", "reasoning": "Needs split", '
            '"subtasks": [{"description": "Build the project by writing /testcase/base_commit_hash and running /src/build.sh", '
            '"config": {"strategy": "heuristic", "base": {"model": "gpt-4o-mini", '
            '"temperature": 0.5, "max_tokens": 4000}, "tool": "openhands"}}]}'
        )
        result = parse_assessment_response(raw, parent_task="Build the project")
        assert result.action == "decompose"

    def test_execute_action_unaffected(self):
        raw = '{"action": "execute", "reasoning": "Simple task"}'
        result = parse_assessment_response(raw, parent_task="anything")
        assert result.action == "execute"

    def test_backward_compatible_without_parent_task(self):
        """Existing callers that don't pass parent_task should still work."""
        raw = (
            '{"action": "decompose", "reasoning": "Split", '
            '"subtasks": [{"description": "Write /testcase/base_commit_hash and run /src/build.sh to compile", '
            '"config": {"strategy": "heuristic", "base": {"model": "gpt-4o-mini", '
            '"temperature": 0.5, "max_tokens": 4000}, "tool": "openhands"}}]}'
        )
        result = parse_assessment_response(raw)
        assert result.action == "decompose"
