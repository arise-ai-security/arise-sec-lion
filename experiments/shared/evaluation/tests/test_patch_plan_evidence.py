"""Tests for strict normalized PatchPlan evidence supplied to semantic judges."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from experiments.shared.evaluation.models import RunData
from experiments.shared.evaluation.patch_plan import (
    PatchPlan as EvaluationPatchPlan,
    normalized_patch_plan,
)
from experiments.shared.scripts.evaluate_run import _semantic_evidence
from plugins.security.patch_plan import PatchPlan as RuntimePatchPlan


def _payload() -> dict:
    return {
        "evidence_references": ["/testcase/root_cause_analysis.txt"],
        "target_file": "/src/project/vulnerable.c",
        "target_symbol": "parse",
        "base_sha256": hashlib.sha256(b"unsafe();\n").hexdigest(),
        "operations": [
            {
                "kind": "replace",
                "anchor": "unsafe();",
                "replacement": "safe();",
            }
        ],
        "allowed_paths": ["/src/project"],
        "forbidden_paths": ["/testcase"],
        "required_postconditions": ["sanitizer finding absent"],
        "validation_commands": ["secb patch", "secb build", "secb repro"],
    }


def _run_data(run_dir: Path) -> RunData:
    return RunData(
        run_id=uuid4(),
        events=[],
        run_dir=run_dir,
        manifest={},
        cve=None,
    )


def test_evaluation_patch_plan_schema_matches_runtime_schema() -> None:
    # Given/When: The independently defined runtime and evaluation schemas are rendered
    runtime_schema = RuntimePatchPlan.model_json_schema()
    evaluation_schema = EvaluationPatchPlan.model_json_schema()

    # Then: The deliberate mirror cannot silently drift from runtime validation
    assert evaluation_schema == runtime_schema


@pytest.mark.parametrize("location", ("plan", "operation"))
def test_evaluation_patch_plan_rejects_unknown_fields(location: str) -> None:
    # Given: A valid evidence plan with one unknown top-level or nested field
    payload = _payload()
    if location == "plan":
        payload["pre_patch_exploit_identity"] = "sha256:arm-identifying-extra"
    else:
        payload["operations"][0]["arbitrary_key"] = "unvalidated"

    # When/Then: The evaluator rejects rather than normalizes the unknown field away
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvaluationPatchPlan.model_validate(payload)


def test_normalized_patch_plan_emits_only_validated_canonical_fields(tmp_path: Path) -> None:
    # Given: A valid but non-canonical JSON representation with omitted default fields
    path = tmp_path / "patch_plan.json"
    path.write_text(json.dumps(_payload(), indent=4), encoding="utf-8")

    # When: The evaluation-side validator prepares judge evidence
    normalized = normalized_patch_plan(path)

    # Then: Defaults and containers are canonicalized through the strict model
    assert normalized is not None
    assert normalized["schema_version"] == "1"
    assert normalized["operations"][0]["expected_occurrences"] == 1
    assert normalized == EvaluationPatchPlan.model_validate(_payload()).model_dump(mode="json")


def test_normalized_patch_plan_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    # Given: A named pipe at the artifact path with no writer attached
    path = tmp_path / "patch_plan.json"
    os.mkfifo(path)
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from experiments.shared.evaluation.patch_plan import normalized_patch_plan\n"
        "raise SystemExit(0 if normalized_patch_plan(Path(sys.argv[1])) is None else 1)\n"
    )

    # When: A separate process reads it under a hard deadline
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    # Then: The reader fails closed promptly instead of waiting for a FIFO writer
    assert completed.returncode == 0, completed.stderr


def test_semantic_evidence_omits_invalid_raw_patch_plan_text(tmp_path: Path) -> None:
    # Given: A nearly valid plan containing arm identity and arbitrary unvalidated text
    run_dir = tmp_path / "run"
    testcase = run_dir / "testcase"
    testcase.mkdir(parents=True)
    payload = _payload()
    payload["cell"] = "B4"
    payload["unvalidated"] = "MUST-NOT-REACH-JUDGE"
    (testcase / "patch_plan.json").write_text(json.dumps(payload), encoding="utf-8")

    # When: Blinded semantic evidence is assembled
    evidence = _semantic_evidence(_run_data(run_dir), poc_present=False, patch_present=False)

    # Then: Invalid raw text and arm-identifying extras never enter the judge packet
    serialized = json.dumps(evidence, sort_keys=True)
    assert evidence["patch_plan"] is None
    assert "patch_plan_excerpt" not in evidence
    assert "MUST-NOT-REACH-JUDGE" not in serialized
    assert '"cell"' not in serialized
