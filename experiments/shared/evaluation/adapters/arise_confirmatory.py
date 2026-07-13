"""Production launch adapter for paired N1/B4 confirmatory studies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import TypeAdapter, ValidationError

from experiments.shared import harness, runners
from experiments.shared.evaluation.combined_verdict import CombinedVerdict, REPLAY_COUNT
from experiments.shared.evaluation.confirmatory_runner import (
    ConfirmatoryCandidate,
    LaunchOutcome,
)
from experiments.shared.evaluation.cost import cost_completeness_rate, total_cost_usd
from experiments.shared.evaluation.loading import load_run
from experiments.shared.evaluation.official import crash_signature
from experiments.shared.evaluation.regression import load_regression_plans_file
from experiments.shared.scripts._paths import get_repo_root
from experiments.shared.scripts.evaluate_run import run_verdict
from experiments.shared.scripts.validate_manifest import validate_manifest


logger = logging.getLogger(__name__)
_COMBINED_VERDICT_ADAPTER = TypeAdapter(CombinedVerdict)


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _sequence(value: object, label: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} is not a sequence")
    return value


def _boolean(record: dict[str, Any], field: str, label: str) -> bool:
    value = record.get(field)
    if type(value) is not bool:
        raise ValueError(f"{label}.{field} is not a boolean")
    return value


def _integer(record: dict[str, Any], field: str, label: str) -> int:
    value = record.get(field)
    if type(value) is not int:
        raise ValueError(f"{label}.{field} is not an integer")
    return value


def _string(record: dict[str, Any], field: str, label: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{label}.{field} is not a string")
    return value


def _optional_string(record: dict[str, Any], field: str, label: str) -> None:
    value = record.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label}.{field} is not a string or null")


def _validate_crash_signature(value: object, label: str) -> None:
    signature = _record(value, label)
    for field in ("sanitizer_class", "access_kind", "top_application_frame"):
        _optional_string(signature, field, label)


def _validate_mechanical_verdict(value: object, label: str) -> None:
    gate = _record(value, label)
    _boolean(gate, "passed", label)
    _string(gate, "reason", label)


def _validate_command_evidence(value: object, label: str) -> None:
    evidence = _record(value, label)
    argv = _sequence(evidence.get("argv"), f"{label}.argv")
    if any(not isinstance(item, str) for item in argv):
        raise ValueError(f"{label}.argv contains a non-string value")
    _integer(evidence, "exit_code", label)
    signal = evidence.get("signal")
    if signal is not None and type(signal) is not int:
        raise ValueError(f"{label}.signal is not an integer or null")
    for field in (
        "timed_out",
        "sanitizer_detected",
        "final_step_reached",
        "assertion_abort",
        "core_dumped",
    ):
        _boolean(evidence, field, label)
    _string(evidence, "output_sha256", label)


def _validate_mode(value: object, label: str) -> None:
    mode = _record(value, label)
    _validate_mechanical_verdict(mode.get("verdict"), f"{label}.verdict")
    _validate_command_evidence(mode.get("evidence"), f"{label}.evidence")
    _validate_crash_signature(mode.get("crash_signature"), f"{label}.crash_signature")


def _validate_replay(value: object, label: str) -> None:
    replay = _record(value, label)
    _string(replay, "replay_id", label)
    _string(replay, "instance_id", label)
    for field in ("container_id", "image_digest", "base_commit"):
        _optional_string(replay, field, label)
    modes = _record(replay.get("modes"), f"{label}.modes")
    _validate_mode(modes.get("primary"), f"{label}.modes.primary")
    _validate_command_evidence(
        replay.get("invocation_evidence"), f"{label}.invocation_evidence"
    )
    _record(replay.get("raw_reports"), f"{label}.raw_reports")


def _validate_semantic(
    value: object,
    *,
    semantic_accepted: bool,
) -> bool:
    semantic = _record(value, "combined.semantic")
    if semantic.get("skipped") == "prior_gate_failed":
        if set(semantic) != {"accepted", "available", "skipped"}:
            raise ValueError("skipped semantic verdict has unexpected fields")
        accepted = _boolean(semantic, "accepted", "combined.semantic")
        available = _boolean(semantic, "available", "combined.semantic")
        if accepted or available or semantic_accepted:
            raise ValueError("skipped semantic verdict is internally inconsistent")
        return True

    verdict = _boolean(semantic, "verdict", "combined.semantic")
    available = _boolean(semantic, "available", "combined.semantic")
    valid_votes = _integer(semantic, "valid_votes", "combined.semantic")
    positive_votes = _integer(semantic, "positive_votes", "combined.semantic")
    judge_error = _boolean(semantic, "judge_error", "combined.semantic")
    _string(semantic, "task_id", "combined.semantic")
    _string(semantic, "prompt", "combined.semantic")
    schema = _record(semantic.get("schema"), "combined.semantic.schema")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in schema.items()):
        raise ValueError("combined.semantic.schema is not a string mapping")
    models = _sequence(semantic.get("models"), "combined.semantic.models")
    calls = _sequence(semantic.get("calls"), "combined.semantic.calls")
    if len(models) != 3 or len(calls) != 3 or any(not isinstance(model, str) for model in models):
        raise ValueError("combined.semantic does not contain three model calls")

    normalized_calls: list[dict[str, Any]] = []
    for index, value in enumerate(calls):
        label = f"combined.semantic.calls[{index}]"
        call = _record(value, label)
        _string(call, "model", label)
        _boolean(call, "valid", label)
        vote = call.get("verdict")
        if vote is not None and type(vote) is not bool:
            raise ValueError(f"{label}.verdict is not a boolean or null")
        if not call["valid"] and vote is not None:
            raise ValueError(f"{label} has a vote marked invalid")
        _record(call.get("response"), f"{label}.response")
        _string(call, "started_at", label)
        _string(call, "finished_at", label)
        normalized_calls.append(call)

    if tuple(call["model"] for call in normalized_calls) != tuple(models):
        raise ValueError("combined.semantic calls do not match the declared models")

    computed_valid = sum(call["valid"] is True for call in normalized_calls)
    computed_positive = sum(call.get("verdict") is True for call in normalized_calls)
    expected_available = computed_valid == 3
    expected_verdict = expected_available and computed_positive >= 2
    if (
        valid_votes != computed_valid
        or positive_votes != computed_positive
        or available != expected_available
        or judge_error != (not expected_available)
        or verdict != expected_verdict
        or semantic_accepted != verdict
    ):
        raise ValueError("combined.semantic aggregation is internally inconsistent")
    return False


def _authoritative_success(verdict: dict[str, object]) -> tuple[bool, bool]:
    """Validate the authoritative envelope before accepting its success flags."""
    if verdict.get("authoritative") != "combined_verdict":
        raise ValueError("evaluator did not return the authoritative combined verdict")
    success = verdict.get("success")
    combined = verdict.get("combined")
    if not isinstance(success, bool) or not isinstance(combined, dict):
        raise ValueError("authoritative evaluator envelope is incomplete")

    combined_success = combined.get("success")
    if type(combined_success) is not bool or combined_success != success:
        raise ValueError("authoritative evaluator success fields disagree")
    mechanical = _record(combined.get("mechanical"), "combined.mechanical")
    mechanical_passed = _boolean(mechanical, "passed", "combined.mechanical")
    if _integer(mechanical, "replay_count", "combined.mechanical") != REPLAY_COUNT:
        raise ValueError("authoritative mechanical verdict lacks three replays")
    _validate_crash_signature(
        mechanical.get("expected_crash_signature"),
        "combined.mechanical.expected_crash_signature",
    )
    _validate_mechanical_verdict(mechanical.get("poc"), "combined.mechanical.poc")
    _validate_mechanical_verdict(
        mechanical.get("patch_primary"), "combined.mechanical.patch_primary"
    )
    sensitivity = _record(
        mechanical.get("sensitivity"), "combined.mechanical.sensitivity"
    )
    for field in ("strict", "generous"):
        _validate_mechanical_verdict(
            sensitivity.get(field), f"combined.mechanical.sensitivity.{field}"
        )

    safety = _record(combined.get("safety"), "combined.safety")
    safety_passed = _boolean(safety, "passed", "combined.safety")
    reasons = _sequence(safety.get("reasons"), "combined.safety.reasons")
    if any(not isinstance(reason, str) for reason in reasons):
        raise ValueError("combined.safety.reasons contains a non-string value")

    regression = _record(combined.get("regression"), "combined.regression")
    regression_available = _boolean(regression, "available", "combined.regression")
    regression_passed = _boolean(regression, "passed", "combined.regression")
    semantic_accepted = _boolean(combined, "semantic_accepted", "combined")
    semantic_skipped = _validate_semantic(
        combined.get("semantic"), semantic_accepted=semantic_accepted
    )

    for field in ("poc_replays", "patch_replays"):
        replays = _sequence(combined.get(field), f"combined.{field}")
        if len(replays) != REPLAY_COUNT:
            raise ValueError(f"authoritative {field} evidence is incomplete")
        for index, replay in enumerate(replays):
            _validate_replay(replay, f"combined.{field}[{index}]")

    normalized = dict(combined)
    normalized_mechanical = dict(mechanical)
    normalized_mechanical["patch_strict"] = sensitivity["strict"]
    normalized_mechanical["patch_generous"] = sensitivity["generous"]
    normalized_mechanical.pop("sensitivity", None)
    normalized["mechanical"] = normalized_mechanical
    if semantic_skipped:
        normalized["semantic"] = None
    try:
        _COMBINED_VERDICT_ADAPTER.validate_python(normalized)
    except ValidationError as exc:
        raise ValueError(
            f"authoritative combined verdict violates its schema: {exc}"
        ) from exc

    expected_success = (
        mechanical_passed
        and safety_passed
        and regression_available
        and regression_passed
        and semantic_accepted
    )
    if success != expected_success:
        raise ValueError("authoritative evaluator gates disagree with combined success")
    return mechanical_passed, success


class AriseConfirmatoryAdapter:
    """Launch declared study cells and resolve every run into an ITT outcome."""

    def __init__(
        self,
        *,
        study_id: str,
        n1_cell: str,
        b4_cell: str,
        repo_root: Path | None = None,
    ) -> None:
        self._study_id = study_id
        self._n1_cell = n1_cell
        self._b4_cell = b4_cell
        self._repo_root = repo_root or get_repo_root()
        study_root = self._repo_root / "experiments" / study_id
        self._study_root = study_root
        self._manifest = self._read_yaml(study_root / "manifest.yaml")
        validate_manifest(self._manifest, repo_root=self._repo_root)
        self._preregistration = self._read_yaml(study_root / "preregistration.yaml")
        if (
            self._preregistration.get("status") != "frozen"
            or not self._preregistration.get("frozen_at")
        ):
            raise ValueError("confirmatory preregistration is draft; freeze it before launch")
        dataset_path = study_root / str(self._manifest["dataset"])
        self._dataset = self._read_yaml(dataset_path)
        self._coverage = harness.ensure_task_coverage(self._dataset)
        self._validate_unseen_cohort()
        cells = self._manifest.get("cells") or {}
        missing = {n1_cell, b4_cell} - set(cells)
        if missing:
            raise ValueError(f"confirmatory cells are not declared: {sorted(missing)}")
        if self._preregistration.get("arms") != [n1_cell, b4_cell]:
            raise ValueError("preregistered arms do not match the requested confirmatory cells")
        self._validate_oracles()
        plans_path = study_root / str(
            self._preregistration.get("regression", {}).get(
                "plan_file", "regression_plans.yaml"
            )
        )
        self._regression_plans_path = plans_path
        plans = load_regression_plans_file(plans_path)
        plan_set_sha256 = hashlib.sha256(
            "\n".join(plan.plan_sha256 for plan in plans.values()).encode("utf-8")
        ).hexdigest()
        validation_path = plans_path.with_name("regression_plan_validation.json")
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if (
            not isinstance(validation, dict)
            or validation.get("plan_set_sha256") != plan_set_sha256
        ):
            raise ValueError("regression validation evidence does not match the frozen plans")
        expected = set(self.instances())
        actual = set(plans)
        if actual != expected:
            raise ValueError(
                "frozen regression plan cohort differs from launch cohort; "
                f"missing={sorted(expected - actual)}; extra={sorted(actual - expected)}"
            )

    def _validate_oracles(self) -> None:
        """Fail before launch if any cohort fixture lacks an exact crash signature."""
        incomplete: list[str] = []
        for task, path in sorted(self._coverage.items()):
            data = json.loads(path.read_text(encoding="utf-8"))
            report = data.get("sanitizer_report") if isinstance(data, dict) else None
            signature = crash_signature(report if isinstance(report, str) else "")
            if not signature.complete or signature.access_kind is None:
                incomplete.append(task)
        if incomplete:
            raise ValueError(
                "confirmatory fixtures lack complete frozen crash signatures: "
                f"{incomplete}"
            )

    def _validate_unseen_cohort(self) -> None:
        """Reject any task recorded in a declared development enrollment."""
        population = self._preregistration.get("population")
        if not isinstance(population, dict):
            raise ValueError("confirmatory population must be a mapping")
        enrollment_files = population.get("development_enrollment_files")
        if not isinstance(enrollment_files, list) or not enrollment_files:
            raise ValueError("confirmatory population declares no development enrollments")

        enrolled_tasks: set[str] = set()
        for relative_path in enrollment_files:
            path = (self._repo_root / str(relative_path)).resolve()
            if not path.is_relative_to(self._repo_root.resolve()):
                raise ValueError(f"development enrollment path escapes repository: {path}")
            document = self._read_yaml(path)
            enrollment = document.get("enrollment")
            if not isinstance(enrollment, list):
                raise ValueError(f"development enrollment is not a list: {path}")
            for item in enrollment:
                task = item.get("task") if isinstance(item, dict) else None
                if not isinstance(task, str):
                    raise ValueError(f"development enrollment has an invalid task: {path}")
                enrolled_tasks.add(task)

        overlap = sorted(set(self._coverage) & enrolled_tasks)
        if overlap:
            raise ValueError(
                "confirmatory cohort overlaps development enrollments; "
                f"replace these tasks before freezing: {overlap}"
            )

    def frozen_parameters(self) -> tuple[int, int, int]:
        """Return preregistered seed, replicates, and bootstrap sample count."""
        allocation = self._preregistration.get("allocation") or {}
        endpoint = self._preregistration.get("primary_endpoint") or {}
        seed = int(allocation["seed"])
        replicates = int(allocation["replicates"])
        bootstrap_samples = int(endpoint["samples"])
        if seed < 0 or replicates < 1 or bootstrap_samples < 1:
            raise ValueError("invalid preregistered allocation or bootstrap parameters")
        return seed, replicates, bootstrap_samples

    def instances(self) -> tuple[tuple[str, str], ...]:
        """Return the exact frozen cohort, rejecting any arm-scope divergence."""
        n1_sequence = harness.resolve_effective_cves(self._dataset, self._n1_cell)
        b4_sequence = harness.resolve_effective_cves(self._dataset, self._b4_cell)
        if len(set(n1_sequence)) != len(n1_sequence):
            raise ValueError("N1 confirmatory task roster contains duplicates")
        if len(set(b4_sequence)) != len(b4_sequence):
            raise ValueError("B4 confirmatory task roster contains duplicates")
        n1_tasks = set(n1_sequence)
        b4_tasks = set(b4_sequence)
        if not n1_tasks or not b4_tasks:
            raise ValueError("both N1 and B4 require a nonempty confirmatory task roster")
        if n1_tasks != b4_tasks:
            raise ValueError(
                "N1/B4 confirmatory task sets differ; "
                f"N1-only={sorted(n1_tasks - b4_tasks)}; "
                f"B4-only={sorted(b4_tasks - n1_tasks)}"
            )
        instances: list[tuple[str, str]] = []
        for task in sorted(n1_tasks):
            path = self._coverage[task]
            data = json.loads(path.read_text(encoding="utf-8"))
            base_commit = data.get("base_commit") if isinstance(data, dict) else None
            if not isinstance(base_commit, str) or not base_commit:
                raise ValueError(f"task {task!r} has no base_commit in {path}")
            instances.append((task, base_commit))
        if not instances:
            raise ValueError("N1 and B4 have no common confirmatory instances")
        return tuple(instances)

    def __call__(self, candidate: ConfirmatoryCandidate) -> LaunchOutcome:
        """Launch one candidate and retain failures under intention-to-treat."""
        if candidate.cell not in {self._n1_cell, self._b4_cell}:
            raise ValueError(f"candidate cell {candidate.cell!r} is not a confirmatory arm")
        cell = self._manifest["cells"][candidate.cell]
        runner = runners.get(str(cell["runner"]))
        config = self._study_root / str(cell["config"])
        context_file = self._coverage[candidate.task]
        try:
            run_id = runner.run(
                study_id=self._study_id,
                cell=candidate.cell,
                task=candidate.task,
                replicate=candidate.replicate,
                config=config,
                context_file=context_file,
            )
        except Exception as exc:
            logger.warning(
                "Confirmatory launch failed for %s/%s: %s",
                candidate.cell,
                candidate.task,
                exc,
            )
            return self._failed_outcome(candidate, status="launch_failed")
        return self._resolve_run(candidate, str(run_id))

    def _resolve_run(self, candidate: ConfirmatoryCandidate, run_id: str) -> LaunchOutcome:
        try:
            run_data = asyncio.run(load_run(run_id))
        except Exception as exc:
            logger.warning("Could not load confirmatory run %s: %s", run_id, exc)
            return self._failed_outcome(
                candidate,
                status="run_load_failed",
                run_id=run_id,
            )
        cost = total_cost_usd(run_data)
        cost_completeness = cost_completeness_rate(run_data)
        status = str(run_data.manifest.get("exit_status") or "unknown")
        # Preregistered arm-independent plans for this confirmatory study.
        try:
            verdict = asyncio.run(
                run_verdict(
                    run_id,
                    regression_plans_path=self._regression_plans_path,
                )
            )
            mechanical_success, combined_success = _authoritative_success(verdict)
        except Exception as exc:
            logger.warning("Could not evaluate confirmatory run %s: %s", run_id, exc)
            return LaunchOutcome(
                cell=candidate.cell,
                task=candidate.task,
                base_commit=candidate.base_commit,
                replicate=candidate.replicate,
                run_id=run_id,
                status="evaluation_failed",
                cost=cost,
                mechanical_success=False,
                combined_success=False,
                cost_completeness_rate=cost_completeness,
                evaluation_complete=False,
            )
        return LaunchOutcome(
            cell=candidate.cell,
            task=candidate.task,
            base_commit=candidate.base_commit,
            replicate=candidate.replicate,
            run_id=run_id,
            status=status,
            cost=cost,
            mechanical_success=mechanical_success,
            combined_success=combined_success,
            cost_completeness_rate=cost_completeness,
            evaluation_complete=True,
        )

    @staticmethod
    def _failed_outcome(
        candidate: ConfirmatoryCandidate,
        *,
        status: str,
        run_id: str | None = None,
    ) -> LaunchOutcome:
        return LaunchOutcome(
            cell=candidate.cell,
            task=candidate.task,
            base_commit=candidate.base_commit,
            replicate=candidate.replicate,
            run_id=run_id,
            status=status,
            cost=None,
            mechanical_success=False,
            combined_success=False,
            cost_completeness_rate=0.0,
            evaluation_complete=False,
        )

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"expected mapping in {path}")
        return data
