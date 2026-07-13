"""Production launch adapter for paired N1/B4 confirmatory studies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from experiments.shared import harness, runners
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
        dataset_path = study_root / str(self._manifest["dataset"])
        self._dataset = self._read_yaml(dataset_path)
        self._coverage = harness.ensure_task_coverage(self._dataset)
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
        completeness = cost_completeness_rate(run_data)
        status = str(run_data.manifest.get("exit_status") or "unknown")
        # Preregistered arm-independent plans for this confirmatory study.
        try:
            verdict = asyncio.run(
                run_verdict(
                    run_id,
                    regression_plans_path=self._regression_plans_path,
                )
            )
            combined = verdict.get("combined")
            combined_record = combined if isinstance(combined, dict) else {}
            mechanical = combined_record.get("mechanical")
            mechanical_record = mechanical if isinstance(mechanical, dict) else {}
            mechanical_success = mechanical_record.get("passed") is True
            combined_success = verdict.get("success") is True
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
                completeness_rate=completeness,
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
            completeness_rate=completeness,
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
            completeness_rate=0.0,
        )

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"expected mapping in {path}")
        return data
