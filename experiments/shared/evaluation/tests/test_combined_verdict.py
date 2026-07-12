"""Tests for the three-layer combined verdict and its authoritative wiring.

Final business success is ``official mechanical AND Arise safety/provenance floor
AND independent semantic``. These tests inject fakes for the two live-infra
seams (the fresh-container replay runner and the LLM judge) so the composition is
verified without Docker or a live model.
"""

from pathlib import Path
from uuid import uuid4

from experiments.shared.evaluation.combined_verdict import (
    CombinedVerdictInput,
    evaluate_combined_verdict,
)
from experiments.shared.evaluation.models import RunData
from experiments.shared.evaluation.official import (
    CommandEvidence,
    SafetyFloorInput,
    SecBenchReplayResult,
)
from experiments.shared.scripts.evaluate_run import build_run_verdict


# --------------------------------------------------------------------------- #
# Fakes for the two live-infra seams: container replay + LLM judge.
# --------------------------------------------------------------------------- #


def _poc_evidence(**updates) -> CommandEvidence:
    """A PoC replay that reaches the final step and triggers a sanitizer (pass)."""
    values = {
        "argv": ("secb", "repro"),
        "exit_code": 1,
        "signal": None,
        "timed_out": False,
        "output_sha256": "a" * 64,
        "sanitizer_detected": True,
        "final_step_reached": True,
    }
    values.update(updates)
    return CommandEvidence(**values)


def _patch_evidence(**updates) -> CommandEvidence:
    """A patched replay that completes cleanly with no sanitizer report (pass)."""
    values = {
        "argv": ("secb", "patch"),
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "output_sha256": "b" * 64,
        "sanitizer_detected": False,
        "final_step_reached": True,
    }
    values.update(updates)
    return CommandEvidence(**values)


class _FakeReplayRunner:
    """Stand-in for :class:`SecBenchReplayRunner`; returns preloaded host evidence."""

    def __init__(self, poc_evidence: CommandEvidence, patch_evidence: CommandEvidence) -> None:
        self._poc = poc_evidence
        self._patch = patch_evidence
        self.evaluation_types: list[str] = []

    def run(self, *, input_dir, output_dir, evaluation_type):
        del input_dir, output_dir
        self.evaluation_types.append(evaluation_type)
        evidence = self._poc if evaluation_type == "poc" else self._patch
        reports = (
            {"sanitizer": ()}
            if evaluation_type == "poc"
            else {"strict": (), "medium": (), "generous": ()}
        )
        return SecBenchReplayResult(
            reports=reports,
            argv=evidence.argv,
            exit_code=evidence.exit_code,
            output_sha256=evidence.output_sha256,
            command_evidence=evidence,
        )


class _FakeSemanticJudge:
    """One blinded semantic judge returning a fixed verdict (or a failing call)."""

    def __init__(self, verdict) -> None:
        self._verdict = verdict

    def __call__(self, built):
        del built
        if self._verdict == "error":
            return {"verdict": False, "_error": True}
        return {"verdict": self._verdict, "reason": "test"}


def _judge_factory(verdicts):
    pending = iter(verdicts)
    return lambda: _FakeSemanticJudge(next(pending))


def _safety(**updates) -> SafetyFloorInput:
    values = {
        "workspace_root": "/sealed/run",
        "artifact_paths": ("/sealed/run/testcase/poc",),
        "artifact_hashes": {"/sealed/run/testcase/poc": "e3b0c442"},
        "modified_paths": ("/sealed/run/src/project/fix.c",),
        "forbidden_paths": (),
        "fresh_base": True,
        "pre_patch_exploit_identity": "same",
        "post_patch_exploit_identity": "same",
    }
    values.update(updates)
    return SafetyFloorInput(**values)


def _request(**updates) -> CombinedVerdictInput:
    values = {
        "poc_input_dir": Path("/in/poc"),
        "poc_output_dir": Path("/out/poc"),
        "patch_input_dir": Path("/in/patch"),
        "patch_output_dir": Path("/out/patch"),
        "poc_present": True,
        "patch_present": True,
        "expected_exit_code": None,
        "safety": _safety(),
        "task_id": "cve-0001",
        "semantic_evidence": {"root_cause": "blinded evidence"},
    }
    values.update(updates)
    return CombinedVerdictInput(**values)


# --------------------------------------------------------------------------- #
# Composition: success only when all three layers pass.
# --------------------------------------------------------------------------- #


def test_all_three_layers_passing_yields_combined_success() -> None:
    """Mechanical PASS + safety PASS + semantic accepted -> combined success."""

    # Given: A passing PoC + patch replay, a clean safety input, unanimous judges
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence())

    # When: The combined verdict composes all three layers
    result = evaluate_combined_verdict(
        _request(),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
        audit_rate=0,
    )

    # Then: Every layer passed and the combined success is True
    assert result.mechanical.passed
    assert result.safety.passed
    assert result.semantic_accepted
    assert result.success
    # And: Both replay evaluation types ran (PoC-primary + patch-primary)
    assert runner.evaluation_types == ["poc", "patch"]


def test_mechanical_failure_fails_combined() -> None:
    """A failing patch interpretation sinks the combined verdict even if others pass."""

    # Given: A patched replay that exits non-zero with no dataset oracle to rescue it
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence(exit_code=1))

    # When: The combined verdict runs (safety + semantic still pass)
    result = evaluate_combined_verdict(
        _request(expected_exit_code=None),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
        audit_rate=0,
    )

    # Then: Mechanical is the sole failing layer and combined success is False
    assert not result.mechanical.passed
    assert result.safety.passed
    assert result.semantic_accepted
    assert not result.success


def test_safety_floor_failure_fails_combined() -> None:
    """A provenance violation (not a fresh base) sinks the combined verdict."""

    # Given: A mechanically-passing run whose patch did not start from a fresh base
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence())

    # When: The combined verdict runs with a broken safety floor input
    result = evaluate_combined_verdict(
        _request(safety=_safety(fresh_base=False)),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
        audit_rate=0,
    )

    # Then: Safety is the sole failing layer and combined success is False
    assert result.mechanical.passed
    assert not result.safety.passed
    assert result.semantic_accepted
    assert not result.success


def test_semantic_rejection_fails_combined() -> None:
    """A rejected/failed semantic gate sinks the combined verdict."""

    # Given: A mechanically-passing, safety-clean run with a rejecting judge panel
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence())

    # When: The combined verdict runs with a unanimous-False panel
    result = evaluate_combined_verdict(
        _request(),
        replay_runner=runner,
        judge_factory=_judge_factory([False, False, False]),
        audit_rate=0,
    )

    # Then: Semantic is the sole failing layer and combined success is False
    assert result.mechanical.passed
    assert result.safety.passed
    assert not result.semantic_accepted
    assert not result.success


def test_semantic_judge_error_fails_closed() -> None:
    """A judge error fails the semantic layer closed (never an implicit accept)."""

    # Given: A panel where one judge call errors out
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence())

    # When: The combined verdict runs
    result = evaluate_combined_verdict(
        _request(),
        replay_runner=runner,
        judge_factory=_judge_factory([True, "error", True]),
        audit_rate=0,
    )

    # Then: The semantic layer is not accepted and combined success is False
    assert result.semantic.judge_error
    assert not result.semantic_accepted
    assert not result.success


def test_patch_strict_and_generous_recorded_as_sensitivity() -> None:
    """Primary is medium; strict/generous are recorded but do not gate success."""

    # Given: A patched replay exiting with the dataset oracle code 7
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence(exit_code=7))

    # When: The combined verdict runs with expected exit 7
    result = evaluate_combined_verdict(
        _request(expected_exit_code=7),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
        audit_rate=0,
    )

    # Then: Medium (primary) passes, strict fails, generous passes -- all recorded
    assert result.mechanical.patch_primary.passed
    assert not result.mechanical.patch_strict.passed
    assert result.mechanical.patch_generous.passed
    # And: Mechanical success keys off the medium primary, so the run passes
    assert result.mechanical.passed
    assert result.success


def test_safety_floor_consumes_patch_side_evidence_not_the_poc_sanitizer() -> None:
    """The PoC's required sanitizer report must not fail the post-fix safety floor."""

    # Given: A passing PoC (sanitizer fires) and a clean patched replay
    runner = _FakeReplayRunner(_poc_evidence(sanitizer_detected=True), _patch_evidence())

    # When: The combined verdict runs
    result = evaluate_combined_verdict(
        _request(),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
        audit_rate=0,
    )

    # Then: Safety still passes because it only inspects post-fix patch evidence
    assert result.safety.passed
    assert result.success


def test_combined_verdict_has_no_agent_authored_verdict_channel() -> None:
    """No agent VERDICT file can enter the composition (verdict-file-agnostic)."""

    import dataclasses
    import inspect

    # Given: The composition entrypoint and its input contract
    params = set(inspect.signature(evaluate_combined_verdict).parameters)
    fields = {field.name for field in dataclasses.fields(CombinedVerdictInput)}

    # Then: Neither exposes an agent-authored verdict input
    forbidden = {"verdict", "agent_verdict", "self_report", "claimed", "verdict_file"}
    assert not (params & forbidden)
    assert not (fields & forbidden)


# --------------------------------------------------------------------------- #
# Wiring: combined is authoritative; legacy criteria is a diagnostic only.
# --------------------------------------------------------------------------- #


def _write_minimal_run_dir(run_dir: Path) -> None:
    """Lay down the artifacts the combined-input assembler reads from disk."""
    testcase = run_dir / "testcase"
    testcase.mkdir(parents=True)
    (testcase / "model_patch.diff").write_text(
        "diff --git a/src/project/fix.c b/src/project/fix.c\n"
        "--- a/src/project/fix.c\n"
        "+++ b/src/project/fix.c\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    (testcase / "poc").write_text("payload", encoding="utf-8")
    (testcase / "poc_path.txt").write_text("testcase/poc\n", encoding="utf-8")


def _run_data(run_dir: Path) -> RunData:
    return RunData(run_id=uuid4(), events=[], run_dir=run_dir, manifest={}, cve=None)


def test_wiring_makes_combined_authoritative_and_legacy_diagnostic(tmp_path) -> None:
    """run-verdict success comes from the combined verdict; legacy is diagnostic only."""

    # Given: A minimal run dir whose combined layers all pass, but whose legacy
    # contract-only gates fail (most required deliverables are absent)
    run_dir = tmp_path / "run"
    _write_minimal_run_dir(run_dir)
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence())

    # When: The wired run verdict is built with injected fakes
    envelope = build_run_verdict(
        _run_data(run_dir),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
        legacy_judge=None,
        strict=False,
    )

    # Then: Authoritative success is the combined verdict's success
    assert envelope["success"] is True
    assert envelope["success"] == envelope["combined"]["success"]
    # And: The legacy criteria verdict is preserved only as a diagnostic
    assert "diagnostic_legacy" in envelope
    assert envelope["diagnostic_legacy"]["overall"] is False
    # And: The legacy failure does NOT override the authoritative combined success
    assert envelope["success"] is not envelope["diagnostic_legacy"]["overall"]


def test_wiring_success_is_agnostic_to_an_agent_verdict_file(tmp_path) -> None:
    """Dropping an agent VERDICT file into the run must not move authoritative success."""

    # Given: A run dir that yields a passing combined verdict
    run_dir = tmp_path / "run"
    _write_minimal_run_dir(run_dir)
    runner = _FakeReplayRunner(_poc_evidence(), _patch_evidence())
    baseline = build_run_verdict(
        _run_data(run_dir),
        replay_runner=_FakeReplayRunner(_poc_evidence(), _patch_evidence()),
        judge_factory=_judge_factory([True, True, True]),
    )

    # When: An agent-authored VERDICT claiming failure is planted, then re-scored
    (run_dir / "testcase" / "VERDICT").write_text("SUCCESS: false\n", encoding="utf-8")
    (run_dir / "VERDICT.json").write_text('{"success": false}\n', encoding="utf-8")
    replanted = build_run_verdict(
        _run_data(run_dir),
        replay_runner=runner,
        judge_factory=_judge_factory([True, True, True]),
    )

    # Then: The authoritative success is unchanged by the agent verdict file
    assert baseline["success"] is True
    assert replanted["success"] == baseline["success"]
