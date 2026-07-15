"""Tests for the operational confirmatory runner (pair, interleave, ITT, report)."""

from itertools import groupby
from pathlib import Path
from uuid import uuid4

import pytest

from config.overlay import resolve_overlay
from experiments.shared.evaluation.confirmatory import PairedObservation
from experiments.shared.evaluation.adapters import arise_confirmatory
from experiments.shared.evaluation.adapters.arise_confirmatory import (
    AriseConfirmatoryAdapter,
)
from experiments.shared.evaluation.confirmatory_runner import (
    ConfirmatoryCandidate,
    LaunchOutcome,
    cost_endpoints_from_outcomes,
    curate_latest_success,
    enroll_intention_to_treat,
    interleaved_launch_order,
    pair_by_instance,
    run_confirmatory_study,
    to_paired_observations,
)
from experiments.shared.evaluation.models import RunData
from experiments.shared.evaluation.regression import RegressionCommand, plan_sha256


def _n1_control_config() -> dict[str, object]:
    return {
        "orchestration": {
            "mode": "flat",
            "procedural_dispatch": False,
            "skip_judge": True,
        },
        "worker": {
            "tool": "openhands",
            "tool_params": {"openhands": {"enable_subagents": False}},
        },
    }


def _outcome(
    cell: str,
    task: str,
    *,
    base_commit: str = "c0",
    replicate: int = 0,
    status: str = "completed",
    cost: float | None = 1.0,
    mechanical: bool = True,
    combined: bool = True,
    cost_completeness: float = 1.0,
    evaluation_complete: bool = True,
) -> LaunchOutcome:
    return LaunchOutcome(
        cell=cell,
        task=task,
        base_commit=base_commit,
        replicate=replicate,
        run_id=f"{cell}-{task}-{replicate}",
        status=status,
        cost=cost,
        mechanical_success=mechanical,
        combined_success=combined,
        cost_completeness_rate=cost_completeness,
        evaluation_complete=evaluation_complete,
    )


def _command_evidence() -> dict[str, object]:
    return {
        "argv": ["secb", "evaluate"],
        "exit_code": 0,
        "signal": None,
        "timed_out": False,
        "output_sha256": "a" * 64,
        "sanitizer_detected": False,
        "final_step_reached": True,
        "assertion_abort": False,
        "core_dumped": False,
    }


def _replay(index: int, *, identities_available: bool) -> dict[str, object]:
    evidence = _command_evidence()
    return {
        "modes": {
            "primary": {
                "verdict": {"passed": True, "reason": "host replay completed"},
                "evidence": evidence,
                "crash_signature": {
                    "sanitizer_class": "heap-buffer-overflow",
                    "access_kind": "read",
                    "top_application_frame": "vulnerable",
                },
            }
        },
        "invocation_evidence": evidence,
        "raw_reports": {"raw": [{"logs": "host output"}]},
        "replay_id": f"replay-{index}",
        "instance_id": "project.cve-0000-0000",
        "container_id": f"container-{index}" if identities_available else None,
        "image_digest": "sha256:deadbeef" if identities_available else None,
        "base_commit": "base" if identities_available else None,
    }


def _regression(*, passed: bool) -> dict[str, object]:
    command = RegressionCommand(argv=("project-smoke",), timeout_seconds=5.0)
    work_dir = "/src/project"
    plan = {
        "instance_id": "project.cve-0000-0000",
        "base_commit": "base",
        "commands": [
            {
                "argv": list(command.argv),
                "timeout_seconds": command.timeout_seconds,
                "required": command.required,
                "cwd": command.cwd,
            }
        ],
        "plan_sha256": plan_sha256(
            "project.cve-0000-0000",
            "base",
            (command,),
            work_dir=work_dir,
            image_digest="sha256:deadbeef",
        ),
        "generated_at": "2026-07-13T00:00:00Z",
        "work_dir": work_dir,
        "generator_model": None,
        "dataset_revision": None,
        "image_digest": "sha256:deadbeef",
        "base_validation_sha256": None,
        "gold_validation_sha256": None,
    }
    return {
        "available": True,
        "passed": passed,
        "reason": "host regression complete",
        "plan": plan,
        "results": [],
        "container_id": "regression-container",
        "image_digest": "sha256:deadbeef",
        "base_commit": "base",
        "patch_hash": "b" * 64,
        "started_at": "2026-07-13T00:00:00Z",
        "finished_at": "2026-07-13T00:00:01Z",
    }


def _semantic(verdict: bool) -> dict[str, object]:
    models = ["judge-a", "judge-b", "judge-c"]
    votes = [verdict, verdict, not verdict]
    return {
        "task_id": "opaque-evaluation",
        "verdict": verdict,
        "available": True,
        "valid_votes": 3,
        "positive_votes": sum(votes),
        "judge_error": False,
        "prompt": "blinded prompt",
        "schema": {"verdict": "bool"},
        "models": models,
        "calls": [
            {
                "model": model,
                "valid": True,
                "verdict": vote,
                "response": {"verdict": vote, "reason": "test"},
                "started_at": "2026-07-13T00:00:00Z",
                "finished_at": "2026-07-13T00:00:01Z",
            }
            for model, vote in zip(models, votes, strict=True)
        ],
    }


def _authoritative_verdict(
    *,
    mechanical: bool = True,
    safety: bool = True,
    regression: bool = True,
    semantic: bool | None = True,
    identities_available: bool = True,
) -> dict[str, object]:
    semantic_accepted = semantic is True
    success = mechanical and safety and regression and semantic_accepted
    return {
        "authoritative": "combined_verdict",
        "success": success,
        "combined": {
            "success": success,
            "mechanical": {
                "passed": mechanical,
                "artifact_completeness": {
                    "passed": True,
                    "required": ["/testcase/security_report.md"],
                    "missing_or_vacuous": [],
                },
                "replay_count": 3,
                "expected_crash_signature": {
                    "sanitizer_class": "heap-buffer-overflow",
                    "access_kind": "read",
                    "top_application_frame": "vulnerable",
                },
                "poc": {"passed": mechanical, "reason": "PoC replay complete"},
                "patch_primary": {
                    "passed": mechanical,
                    "reason": "patch replay complete",
                },
                "sensitivity": {
                    "strict": {"passed": mechanical, "reason": "strict result"},
                    "generous": {"passed": mechanical, "reason": "generous result"},
                },
            },
            "safety": {
                "passed": safety,
                "reasons": [] if safety else ["replay identity unavailable"],
            },
            "regression": _regression(passed=regression),
            "semantic": (
                _semantic(semantic)
                if semantic is not None
                else {
                    "accepted": False,
                    "available": False,
                    "skipped": "prior_gate_failed",
                }
            ),
            "semantic_accepted": semantic_accepted,
            "poc_replays": [
                _replay(index, identities_available=identities_available)
                for index in range(3)
            ],
            "patch_replays": [
                _replay(index + 3, identities_available=identities_available)
                for index in range(3)
            ],
        },
    }


class _FakeLauncher:
    """Injected launch seam: maps (cell, task) -> (success, cost); records calls."""

    def __init__(self, table: dict[tuple[str, str], tuple[bool, float]]) -> None:
        self._table = table
        self.calls: list[ConfirmatoryCandidate] = []

    def __call__(self, candidate: ConfirmatoryCandidate) -> LaunchOutcome:
        self.calls.append(candidate)
        success, cost = self._table[(candidate.cell, candidate.task)]
        return _outcome(
            candidate.cell,
            candidate.task,
            base_commit=candidate.base_commit,
            replicate=candidate.replicate,
            status="completed" if success else "failed",
            cost=cost,
            mechanical=success,
            combined=success,
        )


def test_pairing_matches_by_task_and_base_commit() -> None:
    # Given: N1 and B4 outcomes for two exact instances
    outcomes = (
        _outcome("N1", "gpac.a", base_commit="c1"),
        _outcome("B4", "gpac.a", base_commit="c1"),
        _outcome("N1", "php.b", base_commit="c2"),
        _outcome("B4", "php.b", base_commit="c2"),
    )

    # When: Pairing by (task, base_commit, replicate)
    pairs = pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")

    # Then: Both instances are retained and matched arm-for-arm
    assert [(p.task, p.base_commit) for p in pairs] == [("gpac.a", "c1"), ("php.b", "c2")]
    # And: Each pair binds the correct arm outcomes
    assert pairs[0].n1.cell == "N1"
    assert pairs[0].b4.cell == "B4"


def test_pairing_requires_matching_base_commit() -> None:
    # Given: Same task but divergent base commits across the two arms
    outcomes = (
        _outcome("N1", "gpac.a", base_commit="c1"),
        _outcome("B4", "gpac.a", base_commit="c2"),
    )

    # When/Then: A base-commit mismatch fails instead of silently dropping the task
    with pytest.raises(ValueError, match="assignment sets differ"):
        pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")


def test_pairing_rejects_missing_arm() -> None:
    # Given: One assignment has no B4 outcome
    outcomes = (
        _outcome("N1", "gpac.a", base_commit="c1"),
        _outcome("B4", "gpac.a", base_commit="c1"),
        _outcome("N1", "lonely.c", base_commit="c3"),
    )

    # When/Then: The unequal arm assignment sets fail explicitly
    with pytest.raises(ValueError, match="assignment sets differ"):
        pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")


def test_pairing_rejects_duplicate_arm_outcome() -> None:
    # Given: N1 has two outcomes for the same frozen assignment
    outcomes = (
        _outcome("N1", "gpac.a", base_commit="c1"),
        _outcome("N1", "gpac.a", base_commit="c1"),
        _outcome("B4", "gpac.a", base_commit="c1"),
    )

    # When/Then: Duplicate launches fail instead of choosing or skipping one
    with pytest.raises(ValueError, match="exactly one outcome per arm"):
        pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")


def test_pairing_rejects_empty_outcomes() -> None:
    # Given: No arm outcomes
    outcomes = ()

    # When/Then: An empty confirmatory result cannot be reported
    with pytest.raises(ValueError, match="at least one outcome per arm"):
        pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")


def test_interleave_is_deterministic_for_a_seed() -> None:
    # Given: Five instances and two arms
    instances = [("t1", "c"), ("t2", "c"), ("t3", "c"), ("t4", "c"), ("t5", "c")]

    # When: The order is built twice with the same seed and once with another
    first = interleaved_launch_order(instances, n1_cell="N1", b4_cell="B4", seed=42)
    same = interleaved_launch_order(instances, n1_cell="N1", b4_cell="B4", seed=42)
    other = interleaved_launch_order(instances, n1_cell="N1", b4_cell="B4", seed=43)

    # Then: The same seed reproduces the exact sequence; a different seed diverges
    assert first == same
    assert first != other


def test_interleave_covers_both_arms_and_never_runs_an_arm_thrice() -> None:
    # Given: Three instances at two replicates each
    instances = [("t1", "c"), ("t2", "c"), ("t3", "c")]

    # When: The interleaved order is produced
    order = interleaved_launch_order(
        instances, n1_cell="N1", b4_cell="B4", seed=3, replicates=2
    )
    cells = [candidate.cell for candidate in order]

    # Then: Every (task, replicate, arm) appears exactly once
    keys = {(c.task, c.replicate, c.cell) for c in order}
    assert len(keys) == len(order) == 12
    # And: The two arms are balanced
    assert cells.count("N1") == cells.count("B4") == 6
    # And: No arm ever runs three launches in a row (interleave guarantee)
    longest_run = max(len(list(group)) for _, group in groupby(cells))
    assert longest_run <= 2


def test_interleave_rejects_empty_or_duplicate_instances() -> None:
    # Given: An empty roster and a roster with the same frozen instance twice
    duplicate = [("t1", "c1"), ("t1", "c1")]

    # When/Then: Neither can produce a confirmatory launch schedule
    with pytest.raises(ValueError, match="at least one confirmatory instance"):
        interleaved_launch_order((), n1_cell="N1", b4_cell="B4", seed=1)
    with pytest.raises(ValueError, match="must be unique"):
        interleaved_launch_order(duplicate, n1_cell="N1", b4_cell="B4", seed=1)


def test_intention_to_treat_retains_failed_launch() -> None:
    # Given: N1 passes an instance while B4's only launch failed
    outcomes = (
        _outcome("N1", "gpac.a", base_commit="c1", cost=1.0),
        _outcome(
            "B4",
            "gpac.a",
            base_commit="c1",
            status="failed",
            cost=1.5,
            mechanical=False,
            combined=False,
        ),
    )

    # When: Intention-to-treat enrolls every launch
    itt = enroll_intention_to_treat(outcomes)

    # Then: The failed B4 launch is retained, not curated away
    assert any(o.cell == "B4" and not o.combined_success for o in itt)
    # And: The rejected latest-success curation would have dropped it entirely,
    #      biasing B4's success rate upward
    curated = curate_latest_success(outcomes)
    assert all(o.combined_success for o in curated)
    assert not any(o.cell == "B4" for o in curated)
    # And: ITT yields the honest (pass, fail) paired observation
    pairs = pair_by_instance(itt, n1_cell="N1", b4_cell="B4")
    assert to_paired_observations(pairs) == (PairedObservation(True, False, 1.0, 1.5),)


def test_paired_observations_projection_honours_success_kind() -> None:
    # Given: A pair where mechanical and combined verdicts disagree for B4
    outcomes = (
        _outcome("N1", "t", cost=1.0, mechanical=True, combined=True),
        _outcome("B4", "t", cost=2.0, mechanical=True, combined=False),
    )
    pairs = pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")

    # When/Then: The success kind selects which verdict feeds the statistics
    assert to_paired_observations(pairs, success="combined") == (
        PairedObservation(True, False, 1.0, 2.0),
    )
    assert to_paired_observations(pairs, success="mechanical") == (
        PairedObservation(True, True, 1.0, 2.0),
    )


def test_cost_endpoints_from_outcomes_reports_all_fields() -> None:
    # Given: Two B4 outcomes, one combined-success and one failure
    outcomes = (
        _outcome(
            "B4", "t1", cost=1.6, mechanical=True, combined=True, cost_completeness=1.0
        ),
        _outcome(
            "B4",
            "t2",
            cost=1.4,
            mechanical=False,
            combined=False,
            cost_completeness=0.5,
        ),
    )

    # When: The per-arm cost endpoints are assembled
    endpoints = cost_endpoints_from_outcomes(outcomes)

    # Then: All four predeclared report fields are present and retain failures
    assert endpoints.cost_per_run == pytest.approx((1.6 + 1.4) / 2)
    assert endpoints.cost_per_mechanical_success == pytest.approx(3.0 / 1)
    assert endpoints.cost_per_combined_success == pytest.approx(3.0 / 1)
    assert endpoints.completeness_rate == pytest.approx((1.0 + 0.5) / 2)


def test_cost_endpoints_none_when_no_success() -> None:
    # Given: Every launch failed
    outcomes = (
        _outcome("B4", "t1", cost=1.0, mechanical=False, combined=False),
        _outcome("B4", "t2", cost=2.0, mechanical=False, combined=False),
    )

    # When/Then: Cost-per-success is None (undefined), but cost/run still reports
    endpoints = cost_endpoints_from_outcomes(outcomes)
    assert endpoints.cost_per_mechanical_success is None
    assert endpoints.cost_per_combined_success is None
    assert endpoints.cost_per_run == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("cost", "completeness"),
    [(None, 0.0), (0.0, 0.0)],
)
def test_missing_launch_cost_marks_endpoint_unavailable_not_zero(
    cost: float | None, completeness: float
) -> None:
    # Given: A launch whose cost is missing, including the legacy zero sentinel
    outcomes = (
        _outcome(
            "B4",
            "t1",
            status="launch_failed",
            cost=cost,
            mechanical=False,
            combined=False,
            cost_completeness=completeness,
        ),
    )

    # When: Cost endpoints are assembled under ITT
    endpoints = cost_endpoints_from_outcomes(outcomes)

    # Then: Missing usage stays unavailable — never imputed as 0.0
    assert endpoints.cost_available is False
    assert endpoints.cost_per_run is None
    assert endpoints.missing_assignment_keys == ("B4/t1/c0/r0",)


def test_primary_success_itt_survives_missing_cost() -> None:
    # Given: One pair where B4 failed launch and cost is unknown
    outcomes = (
        _outcome("N1", "gpac.a", base_commit="c1", cost=1.0, combined=True),
        _outcome(
            "B4",
            "gpac.a",
            base_commit="c1",
            status="timeout",
            cost=None,
            mechanical=False,
            combined=False,
            cost_completeness=0.0,
        ),
    )
    pairs = pair_by_instance(outcomes, n1_cell="N1", b4_cell="B4")

    # When: Observations and the study report are assembled
    observations = to_paired_observations(pairs)
    report = run_confirmatory_study(
        [("gpac.a", "c1")],
        n1_cell="N1",
        b4_cell="B4",
        seed=3,
        launch=lambda c: outcomes[0] if c.cell == "N1" else outcomes[1],
        bootstrap_samples=50,
    )

    # Then: Primary ITT success remains computable
    assert observations == (PairedObservation(True, False, 1.0, None),)
    assert report.mcnemar_p == pytest.approx(1.0)
    assert "success_difference" in report.intervals
    assert "cost_ratio" not in report.intervals
    assert report.cost_available is False
    assert report.missing_cost_keys == ("B4/gpac.a/c1/r0",)
    assert report.b4_cost.cost_available is False
    assert report.evaluation_complete
    assert report.evaluation_completeness_rate == 1.0
    assert report.incomplete_evaluation_keys == ()


def test_run_confirmatory_study_injects_launcher_and_assembles_report() -> None:
    # Given: Two instances and an injected fake launcher (no real run execution)
    instances = [("gpac.x", "c1"), ("php.y", "c2")]
    table = {
        ("N1", "gpac.x"): (True, 1.0),
        ("B4", "gpac.x"): (True, 1.6),
        ("N1", "php.y"): (True, 1.2),
        ("B4", "php.y"): (False, 1.4),
    }
    launcher = _FakeLauncher(table)

    # When: The confirmatory study is driven end-to-end through the seam
    report = run_confirmatory_study(
        instances,
        n1_cell="N1",
        b4_cell="B4",
        seed=7,
        launch=launcher,
        bootstrap_samples=200,
    )

    # Then: The injected launcher ran once per (arm, instance)
    assert len(launcher.calls) == 4
    # And: Both instances paired
    assert len(report.pairs) == 2
    # And: Per-arm cost endpoints are computed over retained launches
    assert report.n1_cost.cost_per_run == pytest.approx((1.0 + 1.2) / 2)
    assert report.b4_cost.cost_per_combined_success == pytest.approx((1.6 + 1.4) / 1)
    # And: The predeclared paired statistics are wired in
    assert isinstance(report.mcnemar_p, float)
    assert {"success_difference", "cost_ratio", "cost_per_valid_fix_ratio"} <= set(
        report.intervals
    )
    assert isinstance(report.superiority, bool)
    assert report.evaluation_complete
    assert report.evaluation_completeness_rate == 1.0


@pytest.mark.parametrize("cell", ("N1", "B4"))
def test_production_adapter_resolves_cost_and_authoritative_verdict(
    tmp_path, monkeypatch, cell: str
) -> None:
    # Given: A completed run from either arm and one authoritative combined evaluator.
    run_id = uuid4()
    run_data = RunData(
        run_id=run_id,
        events=[],
        run_dir=tmp_path,
        manifest={"exit_status": "completed"},
    )

    async def fake_load(value):
        assert value == str(run_id)
        return run_data

    async def fake_verdict(value, **kwargs):
        assert value == str(run_id)
        assert kwargs.get("regression_plans_path") == tmp_path / "regression_plans.yaml"
        return _authoritative_verdict()

    monkeypatch.setattr(arise_confirmatory, "load_run", fake_load)
    monkeypatch.setattr(arise_confirmatory, "run_verdict", fake_verdict)
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._study_root = tmp_path
    adapter._regression_plans_path = tmp_path / "regression_plans.yaml"
    candidate = ConfirmatoryCandidate(cell, "task", "base", 0)

    # When: The production adapter resolves the run
    outcome = adapter._resolve_run(candidate, str(run_id))

    # Then: It retains status, cost completeness, and both authoritative gates
    assert outcome.run_id == str(run_id)
    assert outcome.status == "completed"
    assert outcome.cost == 0.0
    assert outcome.cost_completeness_rate == 1.0
    assert outcome.evaluation_complete
    assert outcome.mechanical_success
    assert outcome.combined_success


def test_confirmatory_n1_control_accepts_unassisted_openhands() -> None:
    # Given: the intended flat, single-agent OpenHands control configuration.
    config = _n1_control_config()

    # When + Then: prelaunch control validation accepts it.
    arise_confirmatory._validate_n1_control_config(config)


def test_committed_confirmatory_n1_config_is_unassisted() -> None:
    # Given: the N1 overlay referenced by the paired confirmatory manifest.
    repo_root = Path(__file__).resolve().parents[4]
    study_root = (
        repo_root / "experiments/b4-boss-manager-worker/confirmatory"
    )
    manifest = AriseConfirmatoryAdapter._read_yaml(study_root / "manifest.yaml")
    config_path = study_root / str(manifest["cells"]["N1"]["config"])
    config = resolve_overlay(config_path, repo_root=repo_root)

    # When + Then: its resolved settings satisfy the prelaunch control boundary.
    arise_confirmatory._validate_n1_control_config(config)


@pytest.mark.parametrize(
    ("procedural_dispatch", "enable_subagents", "expected"),
    (
        (True, False, "procedural_dispatch"),
        (None, False, "procedural_dispatch"),
        (False, True, "enable_subagents"),
        (False, None, "enable_subagents"),
    ),
)
def test_confirmatory_n1_control_rejects_in_run_assistance(
    procedural_dispatch: bool | None,
    enable_subagents: bool | None,
    expected: str,
) -> None:
    # Given: an N1 configuration that enables or fails to pin one assistance surface.
    config = _n1_control_config()
    orchestration = config["orchestration"]
    worker = config["worker"]
    assert isinstance(orchestration, dict)
    assert isinstance(worker, dict)
    tool_params = worker["tool_params"]
    assert isinstance(tool_params, dict)
    openhands = tool_params["openhands"]
    assert isinstance(openhands, dict)
    orchestration["procedural_dispatch"] = procedural_dispatch
    openhands["enable_subagents"] = enable_subagents

    # When + Then: prelaunch validation rejects the assisted or implicit setting.
    with pytest.raises(ValueError, match=expected):
        arise_confirmatory._validate_n1_control_config(config)


def test_production_adapter_marks_evaluator_failure_incomplete(
    tmp_path, monkeypatch
) -> None:
    # Given: A run with complete cost records whose authoritative evaluator fails
    run_id = uuid4()
    run_data = RunData(
        run_id=run_id,
        events=[],
        run_dir=tmp_path,
        manifest={"exit_status": "completed"},
    )

    async def fake_load(value):
        assert value == str(run_id)
        return run_data

    async def failed_verdict(value, **kwargs):
        assert value == str(run_id)
        raise RuntimeError("evaluator unavailable")

    monkeypatch.setattr(arise_confirmatory, "load_run", fake_load)
    monkeypatch.setattr(arise_confirmatory, "run_verdict", failed_verdict)
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._regression_plans_path = tmp_path / "regression_plans.yaml"
    candidate = ConfirmatoryCandidate("B4", "task", "base", 0)

    # When: The production adapter resolves the failed evaluation
    outcome = adapter._resolve_run(candidate, str(run_id))

    # Then: Evaluator completeness fails closed without changing cost completeness
    assert outcome.status == "evaluation_failed"
    assert outcome.cost_completeness_rate == 1.0
    assert not outcome.evaluation_complete
    assert not outcome.mechanical_success
    assert not outcome.combined_success


def test_complete_failing_bundle_keeps_evaluation_complete(tmp_path, monkeypatch) -> None:
    # Given: A complete evaluator bundle whose replay identities fail the safety gate
    run_id = uuid4()
    run_data = RunData(
        run_id=run_id,
        events=[],
        run_dir=tmp_path,
        manifest={"exit_status": "completed"},
    )

    async def fake_load(value):
        assert value == str(run_id)
        return run_data

    async def failing_verdict(value, **kwargs):
        assert value == str(run_id)
        return _authoritative_verdict(
            safety=False,
            semantic=None,
            identities_available=False,
        )

    monkeypatch.setattr(arise_confirmatory, "load_run", fake_load)
    monkeypatch.setattr(arise_confirmatory, "run_verdict", failing_verdict)
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._regression_plans_path = tmp_path / "regression_plans.yaml"
    candidate = ConfirmatoryCandidate("B4", "task", "base", 0)

    # When: The production adapter validates the complete failing envelope
    outcome = adapter._resolve_run(candidate, str(run_id))

    # Then: Completeness is distinct from gate success and missing identity values
    assert outcome.status == "completed"
    assert outcome.evaluation_complete
    assert outcome.mechanical_success
    assert not outcome.combined_success


@pytest.mark.parametrize(
    "broken_field",
    (
        "combined",
        "artifact_completeness",
        "safety",
        "regression",
        "semantic",
        "poc_replay",
        "patch_replay",
    ),
)
def test_production_adapter_rejects_incomplete_evaluator_envelope(
    tmp_path, monkeypatch, broken_field
) -> None:
    # Given: Evaluation returns with one malformed authoritative record
    run_id = uuid4()
    run_data = RunData(
        run_id=run_id,
        events=[],
        run_dir=tmp_path,
        manifest={"exit_status": "completed"},
    )

    async def fake_load(value):
        assert value == str(run_id)
        return run_data

    async def incomplete_verdict(value, **kwargs):
        assert value == str(run_id)
        envelope = _authoritative_verdict()
        combined = envelope["combined"]
        assert isinstance(combined, dict)
        if broken_field == "combined":
            envelope["combined"] = {}
        elif broken_field == "artifact_completeness":
            mechanical = combined["mechanical"]
            assert isinstance(mechanical, dict)
            mechanical[broken_field] = {}
        elif broken_field in {"safety", "regression", "semantic"}:
            combined[broken_field] = {}
        else:
            replay_field = "poc_replays" if broken_field == "poc_replay" else "patch_replays"
            replays = combined[replay_field]
            assert isinstance(replays, list)
            replays[0] = {}
        return envelope

    monkeypatch.setattr(arise_confirmatory, "load_run", fake_load)
    monkeypatch.setattr(arise_confirmatory, "run_verdict", incomplete_verdict)
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._regression_plans_path = tmp_path / "regression_plans.yaml"
    candidate = ConfirmatoryCandidate("B4", "task", "base", 0)

    # When: The production adapter validates the returned envelope
    outcome = adapter._resolve_run(candidate, str(run_id))

    # Then: Missing authoritative evidence is an evaluation failure, not a valid failure
    assert outcome.status == "evaluation_failed"
    assert not outcome.evaluation_complete
    assert not outcome.mechanical_success
    assert not outcome.combined_success


def test_incomplete_evaluation_blocks_confirmatory_decision() -> None:
    # Given: One arm has no authoritative evaluator bundle
    outcomes = (
        _outcome("N1", "task", base_commit="base"),
        _outcome(
            "B4",
            "task",
            base_commit="base",
            status="evaluation_failed",
            mechanical=False,
            combined=False,
            evaluation_complete=False,
        ),
    )

    # When: The incomplete assignment is retained under intention to treat
    report = run_confirmatory_study(
        [("task", "base")],
        n1_cell="N1",
        b4_cell="B4",
        seed=7,
        launch=lambda candidate: outcomes[0] if candidate.cell == "N1" else outcomes[1],
        bootstrap_samples=50,
    )

    # Then: The rate and exact key are reported, and no superiority decision is allowed
    assert report.evaluation_completeness_rate == 0.5
    assert not report.evaluation_complete
    assert report.incomplete_evaluation_keys == ("B4/task/base/r0",)
    assert not report.superiority


def test_incomplete_evaluation_cannot_report_success() -> None:
    # Given/When/Then: The outcome model rejects success without evaluator evidence
    with pytest.raises(ValueError, match="incomplete evaluation cannot report success"):
        _outcome("B4", "task", evaluation_complete=False)


def test_production_adapter_dispatches_declared_runner(tmp_path, monkeypatch) -> None:
    # Given: A configured arm, fixture, and registered runner
    calls = []
    run_id = uuid4()

    class FakeRunner:
        def run(self, **kwargs):
            calls.append(kwargs)
            return run_id

    monkeypatch.setattr(arise_confirmatory.runners, "get", lambda _: FakeRunner())
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._study_id = "study"
    adapter._n1_cell = "N1"
    adapter._b4_cell = "B4"
    adapter._study_root = tmp_path
    adapter._manifest = {
        "cells": {"N1": {"runner": "arise", "config": "configs/n1.yaml"}}
    }
    fixture = tmp_path / "task.json"
    fixture.write_text("{}", encoding="utf-8")
    adapter._coverage = {"task": fixture}
    expected = _outcome("N1", "task", base_commit="base")
    monkeypatch.setattr(adapter, "_resolve_run", lambda candidate, value: expected)

    # When: The candidate is launched
    actual = adapter(ConfirmatoryCandidate("N1", "task", "base", 0))

    # Then: The declared runner receives the exact study assignment
    assert actual == expected
    assert calls[0]["study_id"] == "study"
    assert calls[0]["config"] == Path(tmp_path / "configs/n1.yaml")


def test_production_adapter_rejects_unequal_arm_task_sets(tmp_path) -> None:
    fixture_a = tmp_path / "a.json"
    fixture_b = tmp_path / "b.json"
    fixture_a.write_text('{"base_commit": "a"}', encoding="utf-8")
    fixture_b.write_text('{"base_commit": "b"}', encoding="utf-8")
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._n1_cell = "N1"
    adapter._b4_cell = "B4"
    adapter._dataset = {
        "default_cves": ["a", "b"],
        "per_cell_overrides": {"B4": {"subset": ["a"]}},
    }
    adapter._coverage = {"a": fixture_a, "b": fixture_b}

    with pytest.raises(ValueError, match="task sets differ"):
        adapter.instances()


def test_failed_launch_records_unknown_cost() -> None:
    candidate = ConfirmatoryCandidate("N1", "task", "base", 0)

    outcome = AriseConfirmatoryAdapter._failed_outcome(
        candidate, status="launch_failed"
    )

    assert outcome.cost is None
    assert outcome.cost_completeness_rate == 0.0
    assert not outcome.evaluation_complete


def test_production_adapter_reads_frozen_parameters() -> None:
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._preregistration = {
        "allocation": {"seed": 7, "replicates": 2},
        "primary_endpoint": {"samples": 5000},
    }

    assert adapter.frozen_parameters() == (7, 2, 5000)


def test_production_adapter_rejects_incomplete_oracle(tmp_path: Path) -> None:
    fixture = tmp_path / "task.json"
    fixture.write_text('{"sanitizer_report": "no crash signature"}', encoding="utf-8")
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._coverage = {"task": fixture}

    with pytest.raises(ValueError, match="lack complete frozen crash signatures"):
        adapter._validate_oracles()


def test_production_adapter_rejects_development_cohort_overlap(tmp_path: Path) -> None:
    enrollment = tmp_path / "development-enrollment.yaml"
    enrollment.write_text(
        "enrollment:\n- task: already-used\n- task: another-task\n",
        encoding="utf-8",
    )
    adapter = object.__new__(AriseConfirmatoryAdapter)
    adapter._repo_root = tmp_path
    adapter._coverage = {"already-used": tmp_path / "fixture.json"}
    adapter._preregistration = {
        "population": {"development_enrollment_files": [enrollment.name]}
    }

    with pytest.raises(ValueError, match="overlaps development enrollments"):
        adapter._validate_unseen_cohort()


def test_draft_confirmatory_preregistration_cannot_launch() -> None:
    repo_root = Path(__file__).resolve().parents[4]

    with pytest.raises(ValueError, match="preregistration is draft"):
        AriseConfirmatoryAdapter(
            study_id="b4-boss-manager-worker/confirmatory",
            n1_cell="N1",
            b4_cell="B4",
            repo_root=repo_root,
        )
