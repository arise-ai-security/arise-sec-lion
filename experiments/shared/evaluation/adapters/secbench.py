"""Translate Arise artifacts to and from the external SEC-bench evaluator."""

from __future__ import annotations

import base64
import fcntl
import io
import json
import logging
import queue
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from typing import TYPE_CHECKING, Literal

from experiments.shared.evaluation.official import (
    CommandEvidence,
    MechanicalVerdict,
    ReferenceModeResult,
    ReferenceReplayResult,
    command_evidence_from_capture,
    crash_signature,
)


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)

_UPSTREAM_AGENT_FORMAT = "oh"
_TIMEOUT_EXIT_CODE = 137
_REPLAY_LOCK_PATH = "/tmp/arise-secbench-reference-replay.lock"
_SECB_IMAGE_PREFIX = "hwiwonlee/secb.eval.x86_64"
_POC_EXCLUDED_NAMES = frozenset(
    {
        "fix_loop.exit",
        "fix_loop.log",
        "fix_summary.md",
        "model_patch.diff",
        "patch_plan.json",
        "patch_validation_results.txt",
        "security_report.md",
    }
)


class SecBenchArtifactAdapter:
    """Export Arise run artifacts using SEC-bench's OpenHands input schema.

    The schema is only an external transport contract. It does not identify the
    worker that produced the artifacts; OpenHands and Claude Code SDK runs are
    both normalized from Arise's own ``testcase/`` contract before export.
    """

    def export(
        self,
        *,
        run_dir: Path,
        output_root: Path,
        instance_id: str,
    ) -> tuple[Path, Path]:
        """Write isolated PoC and patch inputs and return their directories."""
        if not instance_id.strip():
            raise ValueError("instance_id is required for SEC-bench export")
        testcase = run_dir / "testcase"
        poc_dir = output_root / "inputs" / "poc"
        patch_dir = output_root / "inputs" / "patch"
        poc_dir.mkdir(parents=True, exist_ok=True)
        patch_dir.mkdir(parents=True, exist_ok=True)

        self._write_record(
            poc_dir,
            instance_id=instance_id,
            test_result={"poc_artifact": self._build_poc_archive(testcase)},
        )
        self._write_record(
            patch_dir,
            instance_id=instance_id,
            test_result={"git_patch": self._read_patch(testcase)},
        )
        return poc_dir, patch_dir

    @staticmethod
    def _write_record(
        directory: Path,
        *,
        instance_id: str,
        test_result: dict[str, str],
    ) -> None:
        record = {"instance_id": instance_id, "test_result": test_result}
        (directory / "output.jsonl").write_text(
            json.dumps(record, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _read_patch(testcase: Path) -> str:
        patch = testcase / "model_patch.diff"
        return patch.read_text(encoding="utf-8", errors="replace") if patch.is_file() else ""

    @staticmethod
    def _build_poc_archive(testcase: Path) -> str:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            if testcase.is_dir():
                for path in sorted(testcase.rglob("*")):
                    if not path.is_file() or path.is_symlink():
                        continue
                    if path.name in _POC_EXCLUDED_NAMES or path.name.startswith("fix_run_"):
                        continue
                    relative = path.relative_to(testcase)
                    info = tarfile.TarInfo(relative.as_posix())
                    content = path.read_bytes()
                    info.size = len(content)
                    info.mode = path.stat().st_mode & 0o777
                    info.mtime = 0
                    tar.addfile(info, io.BytesIO(content))
        return base64.b64encode(archive.getvalue()).decode("ascii")


class SecBenchEvaluatorAdapter:
    """Invoke the published SEC-bench evaluator as an external reference."""

    def __init__(
        self,
        secbench_root: Path,
        *,
        dataset: str = "SEC-bench/SEC-bench",
        split: str = "eval",
        timeout_seconds: int = 7200,
        base_commit: str | None = None,
    ) -> None:
        self._script = secbench_root / "secb" / "evaluator" / "eval_instances.py"
        if not self._script.is_file():
            raise FileNotFoundError(f"SEC-bench evaluator not found: {self._script}")
        self._dataset = dataset
        self._split = split
        self._timeout_seconds = timeout_seconds
        self._base_commit = base_commit

    def run(
        self,
        *,
        input_dir: Path,
        output_dir: Path,
        evaluation_type: Literal["poc", "patch"],
    ) -> ReferenceReplayResult:
        """Replay adapted artifacts and return upstream JSONL reports."""
        replay_id = uuid.uuid4().hex
        instance_id = self._read_instance_id(input_dir)
        image = self._image_for(instance_id, evaluation_type)
        mode = "all" if evaluation_type == "patch" else "medium"
        argv = (
            sys.executable,
            str(self._script),
            "--type",
            evaluation_type,
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
            "--dataset",
            self._dataset,
            "--split",
            self._split,
            "--mode",
            mode,
            "--num-workers",
            "1",
            "--agent",
            _UPSTREAM_AGENT_FORMAT,
        )
        timed_out = False
        container_id: str | None = None
        try:
            completed, container_id = self._run_with_container_capture(
                argv=argv,
                cwd=self._script.parents[2],
                image=image,
            )
            exit_code = completed.returncode
            output = completed.stdout + completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = _TIMEOUT_EXIT_CODE
            output = self._stream_text(exc.stdout) + self._stream_text(exc.stderr)
        evidence = command_evidence_from_capture(
            argv=argv,
            exit_code=exit_code,
            output=output,
            timed_out=timed_out,
        )
        if timed_out:
            raise RuntimeError(
                f"SEC-bench evaluator timed out after {self._timeout_seconds}s; "
                f"output_sha256={evidence.output_sha256}"
            )
        if exit_code != 0:
            raise RuntimeError(
                f"SEC-bench evaluator failed with exit {exit_code}; "
                f"output_sha256={evidence.output_sha256}"
            )
        modes = (
            ("strict", "medium", "generous")
            if evaluation_type == "patch"
            else ("sanitizer",)
        )
        reports = {
            report_mode: self._read_jsonl(output_dir / f"report_{report_mode}.jsonl")
            for report_mode in modes
        }
        normalized = {
            self._normalized_mode(evaluation_type, report_mode): self._mode_result(
                evaluation_type=evaluation_type,
                report_mode=report_mode,
                rows=rows,
            )
            for report_mode, rows in reports.items()
        }
        return ReferenceReplayResult(
            modes=normalized,
            invocation_evidence=evidence,
            raw_reports=reports,
            replay_id=replay_id,
            container_id=container_id,
            image_digest=self._image_digest(image),
            base_commit=self._base_commit,
        )

    def _run_with_container_capture(
        self,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        image: str,
    ) -> tuple[subprocess.CompletedProcess[str], str | None]:
        """Run the evaluator while capturing the container id it creates."""
        with open(_REPLAY_LOCK_PATH, "a+", encoding="utf-8") as replay_lock:
            fcntl.flock(replay_lock.fileno(), fcntl.LOCK_EX)
            events: queue.Queue[str] = queue.Queue()
            stop = threading.Event()
            watcher = subprocess.Popen(  # noqa: S603
                [
                    "docker",
                    "events",
                    "--filter",
                    "type=container",
                    "--filter",
                    "event=create",
                    "--format",
                    "{{.ID}} {{.Actor.Attributes.image}}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )

            def _reader() -> None:
                assert watcher.stdout is not None
                for line in watcher.stdout:
                    if stop.is_set():
                        break
                    events.put(line.strip())

            try:
                thread = threading.Thread(target=_reader, daemon=True)
                thread.start()
                time.sleep(0.1)
                completed = subprocess.run(  # noqa: S603 -- argv is host-constructed
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    check=False,
                )
            finally:
                stop.set()
                watcher.terminate()
                try:
                    watcher.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    watcher.kill()
            container_id = self._match_container_event(events, image=image)
            return completed, container_id

    @staticmethod
    def _match_container_event(events: queue.Queue[str], *, image: str) -> str | None:
        """Return the sole matching create event; ambiguity fails closed."""
        matches: list[str] = []
        image_stem = image.split(":")[0]
        while True:
            try:
                line = events.get_nowait()
            except queue.Empty:
                break
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            event_id, event_image = parts
            if image in event_image or image_stem in event_image or event_image in image:
                matches.append(event_id)
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _image_for(instance_id: str, evaluation_type: Literal["poc", "patch"]) -> str:
        tag = "patch" if evaluation_type == "patch" else "poc"
        return f"{_SECB_IMAGE_PREFIX}.{instance_id}:{tag}"

    @staticmethod
    def _image_digest(image: str) -> str | None:
        try:
            completed = subprocess.run(  # noqa: S603
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}",
                    image,
                ],
                capture_output=True,
                text=True,
                timeout=60.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("could not resolve image digest for %s: %s", image, exc)
            return None
        if completed.returncode != 0:
            return None
        digest = (completed.stdout or "").strip()
        return digest or None

    @staticmethod
    def _read_instance_id(input_dir: Path) -> str:
        path = input_dir / "output.jsonl"
        if not path.is_file():
            return "unknown"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and isinstance(row.get("instance_id"), str):
                return row["instance_id"]
        return "unknown"

    @staticmethod
    def _normalized_mode(
        evaluation_type: Literal["poc", "patch"], report_mode: str
    ) -> str:
        if evaluation_type == "poc":
            return "primary"
        return "primary" if report_mode == "medium" else report_mode

    @staticmethod
    def _mode_result(
        *,
        evaluation_type: Literal["poc", "patch"],
        report_mode: str,
        rows: tuple[dict[str, object], ...],
    ) -> ReferenceModeResult:
        if len(rows) != 1:
            raise ValueError(
                f"SEC-bench {evaluation_type}/{report_mode} report must contain exactly "
                f"one row, found {len(rows)}"
            )
        row = rows[0]
        success = row.get("success")
        if not isinstance(success, bool):
            raise ValueError(
                f"SEC-bench {evaluation_type}/{report_mode} report has no boolean success"
            )
        reason_value = row.get("reason")
        reason = reason_value if isinstance(reason_value, str) else "no reason reported"
        exit_value = row.get("exit_code")
        exit_code = exit_value if isinstance(exit_value, int) and not isinstance(exit_value, bool) else -1
        logs_value = row.get("logs")
        logs = logs_value if isinstance(logs_value, str) else ""
        timed_out = exit_code in {124, 137} or "timed out" in reason.lower()
        captured = command_evidence_from_capture(
            argv=("reference-evaluator", evaluation_type, report_mode),
            exit_code=exit_code,
            output=logs,
            timed_out=timed_out,
            final_step_marker="Step 3: Run PoC",
        )
        sanitizer_value = row.get("sanitizer_triggered")
        sanitizer_detected = (
            sanitizer_value if isinstance(sanitizer_value, bool) else captured.sanitizer_detected
        )
        report_evidence = CommandEvidence(
            argv=captured.argv,
            exit_code=captured.exit_code,
            signal=captured.signal,
            timed_out=captured.timed_out,
            output_sha256=captured.output_sha256,
            sanitizer_detected=sanitizer_detected,
            final_step_reached=captured.final_step_reached,
            assertion_abort=captured.assertion_abort,
            core_dumped=captured.core_dumped,
        )
        return ReferenceModeResult(
            verdict=MechanicalVerdict(passed=success, reason=reason),
            evidence=report_evidence,
            crash_signature=crash_signature(logs),
        )

    @staticmethod
    def _stream_text(stream: str | bytes | None) -> str:
        if stream is None:
            return ""
        return stream if isinstance(stream, str) else stream.decode("utf-8", "replace")

    @staticmethod
    def _read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
        if not path.is_file():
            raise FileNotFoundError(f"SEC-bench report missing: {path}")
        return tuple(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
