"""Tests for the external SEC-bench reference adapter."""

import base64
import io
import json
import queue
import subprocess
import tarfile

from experiments.shared.evaluation.adapters.secbench import (
    SecBenchArtifactAdapter,
    SecBenchEvaluatorAdapter,
)
from experiments.shared.evaluation.official import CommandEvidence, ReferenceReplayResult


def test_artifact_adapter_exports_openhands_schema_for_both_replays(tmp_path) -> None:
    # Given: Arise-owned PoC and patch artifacts
    run_dir = tmp_path / "run"
    testcase = run_dir / "testcase"
    testcase.mkdir(parents=True)
    (testcase / "poc.bin").write_bytes(b"trigger")
    (testcase / "poc_path.txt").write_text("/testcase/poc.bin\n", encoding="utf-8")
    (testcase / "repro.sh").write_text("run /testcase/poc.bin\n", encoding="utf-8")
    (testcase / "model_patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")

    # When: They are translated for the external reference evaluator
    poc_dir, patch_dir = SecBenchArtifactAdapter().export(
        run_dir=run_dir,
        output_root=run_dir / "evaluation_replay",
        instance_id="project.cve-0000-0000",
    )

    # Then: SEC-bench receives its OpenHands JSONL transport schema
    poc_record = json.loads((poc_dir / "output.jsonl").read_text(encoding="utf-8"))
    patch_record = json.loads((patch_dir / "output.jsonl").read_text(encoding="utf-8"))
    assert poc_record["instance_id"] == "project.cve-0000-0000"
    assert patch_record["test_result"]["git_patch"] == "diff --git a/x b/x\n"

    # And: The PoC transport is a base64 tar containing only pre-patch artifacts
    archive = base64.b64decode(poc_record["test_result"]["poc_artifact"])
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert {"poc.bin", "poc_path.txt", "repro.sh"}.issubset(names)
    assert "model_patch.diff" not in names


def test_evaluator_adapter_uses_openhands_parser_and_captures_evidence(
    tmp_path, monkeypatch
) -> None:
    # Given: A stub SEC-bench checkout and successful upstream report
    script = tmp_path / "secb" / "evaluator" / "eval_instances.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    (input_dir / "output.jsonl").write_text(
        '{"instance_id": "project.cve-0000-0000", "test_result": {}}\n',
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "report_sanitizer.jsonl").write_text(
        '{"instance_id": "x", "success": true}\n', encoding="utf-8"
    )
    captured = subprocess.CompletedProcess(args=(), returncode=0, stdout="poc ran\n", stderr="")

    def fake_run(*args, **kwargs):
        del kwargs
        argv = args[0] if args else []
        if argv and argv[0] == "docker" and "inspect" in argv:
            return subprocess.CompletedProcess(
                args=(), returncode=0, stdout="sha256:imgdigest\n", stderr=""
            )
        return captured

    class FakePopen:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            self.stdout = iter(
                [
                    "ctrdeadbeef01 hwiwonlee/secb.eval.x86_64.project.cve-0000-0000:poc\n"
                ]
            )

        def terminate(self) -> None:
            return None

        def wait(self, timeout=None) -> int:
            del timeout
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    # When: The external evaluator adapter runs
    result = SecBenchEvaluatorAdapter(
        tmp_path, base_commit="abc123"
    ).run(
        input_dir=input_dir,
        output_dir=output_dir,
        evaluation_type="poc",
    )

    # Then: The compatibility parser is OpenHands and provenance is retained
    assert isinstance(result, ReferenceReplayResult)
    assert isinstance(result.invocation_evidence, CommandEvidence)
    argv = result.invocation_evidence.argv
    assert argv[argv.index("--agent") + 1] == "oh"
    assert result.modes["primary"].verdict.passed
    assert result.container_id == "ctrdeadbeef01"
    assert result.image_digest == "sha256:imgdigest"
    assert result.base_commit == "abc123"
    assert result.replay_id


def test_container_capture_fails_closed_when_matching_events_are_ambiguous() -> None:
    events: queue.Queue[str] = queue.Queue()
    image = "hwiwonlee/secb.eval.x86_64.project.cve-0000-0000:poc"
    events.put(f"ctr-one {image}")
    events.put(f"ctr-two {image}")

    assert SecBenchEvaluatorAdapter._match_container_event(events, image=image) is None
