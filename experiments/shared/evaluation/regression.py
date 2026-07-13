"""Host-owned, preregistered regression execution for confirmatory evaluation.

Worker-authored ``*regression*`` files are never proof. Authority requires a
frozen plan keyed by ``(instance_id, base_commit)`` that is identical across
arms, executed inside a fresh patched SEC-bench container (not on the host),
and fail-closed when missing.

Host-side ``subprocess.run`` of plan argv is intentionally rejected: generated
commands must not run against the host worktree, and only a containerized
execution proves the suite ran on the patched project.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shlex
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol


logger = logging.getLogger(__name__)

_SECB_IMAGE_PREFIX = "hwiwonlee/secb.eval.x86_64"
_DEFAULT_WORK_DIR = "/src"
_TRIVIAL_SHELL_COMMANDS = frozenset({":", "true", "test -d .", "[ -d . ]"})


@dataclass(frozen=True, slots=True)
class RegressionCommand:
    """One frozen shell invocation in a regression plan."""

    argv: tuple[str, ...]
    timeout_seconds: float
    required: bool = True
    cwd: str = "."

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("regression command argv must be non-empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")
        shell = (
            self.argv[2].strip()
            if self.argv[:2] == ("bash", "-lc")
            else " ".join(self.argv)
        )
        normalized_shell = " ".join(shell.split())
        if normalized_shell in _TRIVIAL_SHELL_COMMANDS:
            raise ValueError(f"trivial regression command is not evidence: {shell!r}")


@dataclass(frozen=True, slots=True)
class FrozenRegressionPlan:
    """Arm-independent regression contract frozen before confirmatory launch."""

    instance_id: str
    base_commit: str
    commands: tuple[RegressionCommand, ...]
    plan_sha256: str
    generated_at: str
    work_dir: str
    generator_model: str | None = None
    dataset_revision: str | None = None
    image_digest: str | None = None
    base_validation_sha256: str | None = None
    gold_validation_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("instance_id is required")
        if not self.base_commit.strip():
            raise ValueError("base_commit is required")
        if not self.commands:
            raise ValueError("regression plan requires at least one command")
        if not self.work_dir.startswith("/src/"):
            raise ValueError("regression plan work_dir must be a project directory under /src")
        expected = plan_sha256(
            self.instance_id,
            self.base_commit,
            self.commands,
            work_dir=self.work_dir,
            dataset_revision=self.dataset_revision,
            image_digest=self.image_digest,
            base_validation_sha256=self.base_validation_sha256,
            gold_validation_sha256=self.gold_validation_sha256,
        )
        if self.plan_sha256 != expected:
            raise ValueError(
                f"plan_sha256 mismatch for {self.instance_id}: "
                f"expected {expected}, got {self.plan_sha256}"
            )


@dataclass(frozen=True, slots=True)
class RegressionCommandResult:
    """Captured container evidence for one regression command."""

    argv: tuple[str, ...]
    exit_code: int
    timed_out: bool
    signal: int | None
    output_sha256: str
    duration_seconds: float
    cwd: str
    required: bool


@dataclass(frozen=True, slots=True)
class RegressionEvidence:
    """Authoritative host regression outcome for the combined verdict."""

    available: bool
    passed: bool
    reason: str
    plan: FrozenRegressionPlan | None
    results: tuple[RegressionCommandResult, ...]
    container_id: str | None
    image_digest: str | None
    base_commit: str
    patch_hash: str | None
    started_at: str
    finished_at: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def regression_evidence_sha256(evidence: RegressionEvidence) -> str:
    """Hash stable validation facts while excluding timestamps and container IDs."""
    payload = {
        "available": evidence.available,
        "passed": evidence.passed,
        "reason": evidence.reason,
        "results": [
            {
                "argv": list(result.argv),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "signal": result.signal,
                "output_sha256": result.output_sha256,
                "cwd": result.cwd,
                "required": result.required,
            }
            for result in evidence.results
        ],
        "image_digest": evidence.image_digest,
        "base_commit": evidence.base_commit,
        "patch_hash": evidence.patch_hash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RegressionRunnerPort(Protocol):
    """Execute a frozen plan in a fresh patched SEC-bench container."""

    def run(
        self,
        plan: FrozenRegressionPlan,
        *,
        patch_file: Path | None,
        patch_hash: str | None,
        image: str | None = None,
    ) -> RegressionEvidence: ...


def plan_sha256(
    instance_id: str,
    base_commit: str,
    commands: tuple[RegressionCommand, ...] | list[RegressionCommand],
    *,
    work_dir: str = _DEFAULT_WORK_DIR,
    dataset_revision: str | None = None,
    image_digest: str | None = None,
    base_validation_sha256: str | None = None,
    gold_validation_sha256: str | None = None,
) -> str:
    """Stable hash over the arm-independent plan identity and command list."""
    body = {
        "instance_id": instance_id,
        "base_commit": base_commit,
        "work_dir": work_dir,
        "dataset_revision": dataset_revision,
        "image_digest": image_digest,
        "base_validation_sha256": base_validation_sha256,
        "gold_validation_sha256": gold_validation_sha256,
        "commands": [
            {
                "argv": list(cmd.argv),
                "timeout_seconds": cmd.timeout_seconds,
                "required": cmd.required,
                "cwd": cmd.cwd,
            }
            for cmd in commands
        ],
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def unavailable_regression(
    *,
    reason: str,
    base_commit: str = "",
    plan: FrozenRegressionPlan | None = None,
    started_at: str = "",
    finished_at: str = "",
) -> RegressionEvidence:
    """Fail-closed regression outcome when no plan or runner can execute."""
    return RegressionEvidence(
        available=False,
        passed=False,
        reason=reason,
        plan=plan,
        results=(),
        container_id=None,
        image_digest=None,
        base_commit=base_commit or (plan.base_commit if plan is not None else ""),
        patch_hash=None,
        started_at=started_at,
        finished_at=finished_at,
    )


def load_regression_plan(
    plans: dict[tuple[str, str], FrozenRegressionPlan],
    *,
    instance_id: str,
    base_commit: str,
) -> FrozenRegressionPlan | None:
    """Look up a frozen plan by the confirmatory pairing key."""
    return plans.get((instance_id, base_commit))


def parse_regression_plan_document(document: dict[str, Any]) -> FrozenRegressionPlan:
    """Parse one plan object from a preregistered YAML/JSON document."""
    commands_raw = document.get("commands")
    if not isinstance(commands_raw, list) or not commands_raw:
        raise ValueError("regression plan document requires a non-empty commands list")
    commands: list[RegressionCommand] = []
    for item in commands_raw:
        if not isinstance(item, dict):
            raise ValueError("each regression command must be a mapping")
        argv = item.get("argv")
        if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
            raise ValueError("command argv must be a list of strings")
        commands.append(
            RegressionCommand(
                argv=tuple(argv),
                timeout_seconds=float(item.get("timeout_seconds", 300)),
                required=bool(item.get("required", True)),
                cwd=str(item.get("cwd", ".")),
            )
        )
    instance_id = str(document["instance_id"])
    base_commit = str(document["base_commit"])
    command_tuple = tuple(commands)
    work_dir = str(document.get("work_dir") or _DEFAULT_WORK_DIR)
    dataset_revision = (
        None
        if document.get("dataset_revision") in (None, "")
        else str(document.get("dataset_revision"))
    )
    image_digest = (
        None
        if document.get("image_digest") in (None, "")
        else str(document.get("image_digest"))
    )
    base_validation_sha256 = (
        None
        if document.get("base_validation_sha256") in (None, "")
        else str(document.get("base_validation_sha256"))
    )
    gold_validation_sha256 = (
        None
        if document.get("gold_validation_sha256") in (None, "")
        else str(document.get("gold_validation_sha256"))
    )
    digest = str(
        document.get("plan_sha256")
        or plan_sha256(
            instance_id,
            base_commit,
            command_tuple,
            work_dir=work_dir,
            dataset_revision=dataset_revision,
            image_digest=image_digest,
            base_validation_sha256=base_validation_sha256,
            gold_validation_sha256=gold_validation_sha256,
        )
    )
    return FrozenRegressionPlan(
        instance_id=instance_id,
        base_commit=base_commit,
        commands=command_tuple,
        plan_sha256=digest,
        generated_at=str(document.get("generated_at", "")),
        work_dir=work_dir,
        generator_model=(
            None
            if document.get("generator_model") in (None, "")
            else str(document.get("generator_model"))
        ),
        dataset_revision=dataset_revision,
        image_digest=image_digest,
        base_validation_sha256=base_validation_sha256,
        gold_validation_sha256=gold_validation_sha256,
    )


def load_regression_plans_file(path: Path) -> dict[tuple[str, str], FrozenRegressionPlan]:
    """Load a multi-plan document ``{plans: [...]}`` or a single plan mapping."""
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: regression plan file must be a mapping")
    plans_raw = raw.get("plans", [raw] if "instance_id" in raw else [])
    if not isinstance(plans_raw, list):
        raise ValueError(f"{path}: plans must be a list")
    loaded: dict[tuple[str, str], FrozenRegressionPlan] = {}
    for item in plans_raw:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each plan entry must be a mapping")
        if not item.get("plan_sha256"):
            raise ValueError(f"{path}: each frozen plan requires an explicit plan_sha256")
        plan = parse_regression_plan_document(item)
        _validate_frozen_plan_authority(plan, source=path)
        key = (plan.instance_id, plan.base_commit)
        if key in loaded:
            raise ValueError(f"duplicate regression plan for {key}")
        loaded[key] = plan
    meta = raw.get("meta")
    if not isinstance(meta, dict) or not meta.get("plan_set_sha256"):
        raise ValueError(f"{path}: frozen plan file requires meta.plan_set_sha256")
    actual_plan_set_sha256 = hashlib.sha256(
        "\n".join(plan.plan_sha256 for plan in loaded.values()).encode("utf-8")
    ).hexdigest()
    if meta["plan_set_sha256"] != actual_plan_set_sha256:
        raise ValueError(
            f"{path}: plan_set_sha256 mismatch: "
            f"expected {actual_plan_set_sha256}, got {meta['plan_set_sha256']}"
        )
    return loaded


def _validate_frozen_plan_authority(plan: FrozenRegressionPlan, *, source: Path) -> None:
    """Require provenance that proves a file-backed plan was preregistered and validated."""
    missing = [
        name
        for name, value in (
            ("generated_at", plan.generated_at),
            ("generator_model", plan.generator_model),
            ("dataset_revision", plan.dataset_revision),
            ("image_digest", plan.image_digest),
            ("base_validation_sha256", plan.base_validation_sha256),
            ("gold_validation_sha256", plan.gold_validation_sha256),
        )
        if value is None or not str(value).strip()
    ]
    if missing:
        raise ValueError(
            f"{source}: regression plan {plan.instance_id} lacks frozen authority: {missing}"
        )
    for name, digest in (
        ("plan_sha256", plan.plan_sha256),
        ("base_validation_sha256", plan.base_validation_sha256),
        ("gold_validation_sha256", plan.gold_validation_sha256),
    ):
        value = str(digest)
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError(f"{source}: {name} must be a lowercase SHA-256 digest")


def default_secbench_image(instance_id: str) -> str:
    """Canonical SEC-bench patch-tag image for an instance."""
    return f"{_SECB_IMAGE_PREFIX}.{instance_id}:patch"


def _signal_from_exit(exit_code: int) -> int | None:
    if exit_code < 0:
        return -exit_code
    if exit_code > 128:
        return exit_code - 128
    return None


def _docker(
    args: list[str],
    *,
    timeout: float,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603 — host docker CLI, fixed argv prefixes
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args[:4])} failed ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )
    return completed


def _image_digest(image: str) -> str | None:
    completed = _docker(
        [
            "image",
            "inspect",
            "--format",
            "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}",
            image,
        ],
        timeout=60.0,
    )
    if completed.returncode != 0:
        return None
    digest = (completed.stdout or "").strip()
    return digest or None


def _resolve_container_cwd(work_dir: str, command_cwd: str) -> str | None:
    """Resolve plan cwd under the container work_dir; reject escape."""
    if command_cwd in ("", "."):
        return work_dir
    if command_cwd.startswith("/"):
        # Absolute container paths must stay under work_dir or be work_dir itself.
        work = Path(work_dir)
        target = Path(command_cwd)
        try:
            target.relative_to(work)
        except ValueError:
            if target != work:
                return None
        return command_cwd
    joined = str(Path(work_dir) / command_cwd)
    # Reject path components that climb above work_dir.
    work_parts = Path(work_dir).parts
    joined_parts = Path(joined).parts
    if len(joined_parts) < len(work_parts):
        return None
    if joined_parts[: len(work_parts)] != work_parts:
        return None
    if ".." in Path(command_cwd).parts:
        # Normalize and re-check containment after resolving ..
        normalized = Path(work_dir)
        for part in Path(command_cwd).parts:
            if part == "..":
                if normalized == Path(work_dir) or normalized.parent == normalized:
                    return None
                if not str(normalized).startswith(work_dir.rstrip("/") + "/") and str(
                    normalized
                ) != work_dir.rstrip("/"):
                    return None
                normalized = normalized.parent
                if not (
                    str(normalized) == work_dir.rstrip("/")
                    or str(normalized).startswith(work_dir.rstrip("/") + "/")
                ):
                    return None
            elif part not in ("", "."):
                normalized = normalized / part
        if not (
            str(normalized) == work_dir.rstrip("/")
            or str(normalized).startswith(work_dir.rstrip("/") + "/")
        ):
            return None
        return str(normalized)
    return joined


class ContainerRegressionRunner:
    """Execute frozen regression argv lists inside a fresh patched SEC-bench container.

    Lifecycle per evaluation:
    1. ``docker create`` a new container from the instance's ``:patch`` image.
    2. Optionally copy and apply the model patch, then rebuild.
    3. ``docker exec`` each frozen plan command with its timeout.
    4. ``docker rm -f`` the container (always).
    """

    def __init__(self, *, apply_patch: bool = True, rebuild: bool = True) -> None:
        self._apply_patch = apply_patch
        self._rebuild = rebuild

    def run(
        self,
        plan: FrozenRegressionPlan,
        *,
        patch_file: Path | None,
        patch_hash: str | None,
        image: str | None = None,
    ) -> RegressionEvidence:
        image_ref = image or default_secbench_image(plan.instance_id)
        started = time.time()
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
        container_id: str | None = None
        if self._apply_patch:
            if patch_file is None or not patch_file.is_file() or patch_hash is None:
                return unavailable_regression(
                    reason="patched regression requires a patch file and SHA-256",
                    base_commit=plan.base_commit,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            try:
                actual_patch_hash = hashlib.sha256(patch_file.read_bytes()).hexdigest()
            except OSError as exc:
                return unavailable_regression(
                    reason=f"could not read regression patch: {exc}",
                    base_commit=plan.base_commit,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            if patch_hash != actual_patch_hash:
                return unavailable_regression(
                    reason=(
                        "patch SHA-256 mismatch for regression input: "
                        f"declared={patch_hash} actual={actual_patch_hash}"
                    ),
                    base_commit=plan.base_commit,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
        digest = _image_digest(image_ref)
        if plan.image_digest and digest is None:
            return unavailable_regression(
                reason=f"could not resolve required image digest for {image_ref}",
                base_commit=plan.base_commit,
                plan=plan,
                started_at=started_at,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        if plan.image_digest and plan.image_digest != digest:
            return unavailable_regression(
                reason=(
                    f"image digest mismatch for {image_ref}: "
                    f"plan={plan.image_digest} live={digest}"
                ),
                base_commit=plan.base_commit,
                plan=plan,
                started_at=started_at,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        try:
            create = _docker(
                [
                    "create",
                    "--name",
                    f"arise-reg-{uuid.uuid4().hex[:12]}",
                    "-w",
                    plan.work_dir,
                    image_ref,
                    "sleep",
                    "infinity",
                ],
                timeout=120.0,
            )
            if create.returncode != 0:
                return unavailable_regression(
                    reason=(
                        f"failed to create regression container from {image_ref}: "
                        f"{(create.stderr or create.stdout or '').strip()}"
                    ),
                    base_commit=plan.base_commit,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            container_id = (create.stdout or "").strip()
            if not container_id:
                return unavailable_regression(
                    reason=f"docker create returned empty container id for {image_ref}",
                    base_commit=plan.base_commit,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            start = _docker(["start", container_id], timeout=60.0)
            if start.returncode != 0:
                return unavailable_regression(
                    reason=f"failed to start regression container {container_id[:12]}",
                    base_commit=plan.base_commit,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            head = _docker(
                ["exec", "-w", plan.work_dir, container_id, "git", "rev-parse", "HEAD"],
                timeout=60.0,
            )
            actual_base = (head.stdout or "").strip()
            if head.returncode != 0 or actual_base != plan.base_commit:
                return unavailable_regression(
                    reason=(
                        f"base commit mismatch in regression container: "
                        f"plan={plan.base_commit} actual={actual_base or '<unavailable>'}"
                    ),
                    base_commit=actual_base,
                    plan=plan,
                    started_at=started_at,
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
            if self._apply_patch:
                assert patch_file is not None
                prep = self._prepare_patched_tree(
                    container_id, patch_file=patch_file, work_dir=plan.work_dir
                )
                if prep is not None:
                    return unavailable_regression(
                        reason=prep,
                        base_commit=plan.base_commit,
                        plan=plan,
                        started_at=started_at,
                        finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    )
            if self._rebuild:
                build_error = self._build_tree(container_id, work_dir=plan.work_dir)
                if build_error is not None:
                    return unavailable_regression(
                        reason=build_error,
                        base_commit=plan.base_commit,
                        plan=plan,
                        started_at=started_at,
                        finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    )
            results: list[RegressionCommandResult] = []
            for command in plan.commands:
                cwd = _resolve_container_cwd(plan.work_dir, command.cwd)
                if cwd is None:
                    return unavailable_regression(
                        reason=f"command cwd escapes container work_dir: {command.cwd}",
                        base_commit=plan.base_commit,
                        plan=plan,
                        started_at=started_at,
                        finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    )
                command_started = time.time()
                timed_out = False
                shell = " ".join(shlex.quote(part) for part in command.argv)
                try:
                    exec_result = _docker(
                        [
                            "exec",
                            "-w",
                            cwd,
                            container_id,
                            "bash",
                            "-lc",
                            shell,
                        ],
                        timeout=command.timeout_seconds + 5.0,
                    )
                    exit_code = int(exec_result.returncode)
                    output = (exec_result.stdout or "") + (exec_result.stderr or "")
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    exit_code = 124
                    stdout = exc.stdout or ""
                    stderr = exc.stderr or ""
                    if isinstance(stdout, bytes):
                        stdout = stdout.decode("utf-8", errors="replace")
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode("utf-8", errors="replace")
                    output = str(stdout) + str(stderr)
                duration = time.time() - command_started
                results.append(
                    RegressionCommandResult(
                        argv=command.argv,
                        exit_code=exit_code,
                        timed_out=timed_out,
                        signal=_signal_from_exit(exit_code) if not timed_out else None,
                        output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                        duration_seconds=duration,
                        cwd=command.cwd,
                        required=command.required,
                    )
                )
                if command.required and (timed_out or exit_code != 0):
                    finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    return RegressionEvidence(
                        available=True,
                        passed=False,
                        reason=(
                            f"required regression command failed: argv={list(command.argv)} "
                            f"exit_code={exit_code} timed_out={timed_out}"
                        ),
                        plan=plan,
                        results=tuple(results),
                        container_id=container_id,
                        image_digest=digest,
                        base_commit=plan.base_commit,
                        patch_hash=patch_hash,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
            finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return RegressionEvidence(
                available=True,
                passed=True,
                reason="all required regression commands passed",
                plan=plan,
                results=tuple(results),
                container_id=container_id,
                image_digest=digest,
                base_commit=plan.base_commit,
                patch_hash=patch_hash,
                started_at=started_at,
                finished_at=finished_at,
            )
        except subprocess.TimeoutExpired as exc:
            return unavailable_regression(
                reason=f"docker regression operation timed out: {exc}",
                base_commit=plan.base_commit,
                plan=plan,
                started_at=started_at,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        except OSError as exc:
            return unavailable_regression(
                reason=f"docker regression infrastructure error: {exc}",
                base_commit=plan.base_commit,
                plan=plan,
                started_at=started_at,
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        finally:
            if container_id:
                try:
                    _docker(["rm", "-f", container_id], timeout=60.0)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    logger.warning(
                        "failed to remove regression container %s: %s",
                        container_id[:12],
                        exc,
                    )

    def _prepare_patched_tree(
        self,
        container_id: str,
        *,
        patch_file: Path,
        work_dir: str,
    ) -> str | None:
        """Copy and apply the model patch; optionally rebuild. None on success."""
        copy = _docker(
            ["cp", str(patch_file.resolve()), f"{container_id}:/testcase/model_patch.diff"],
            timeout=60.0,
        )
        if copy.returncode != 0:
            return (
                "failed to copy model patch into regression container: "
                f"{(copy.stderr or copy.stdout or '').strip()}"
            )
        apply = _docker(
            [
                "exec",
                "-w",
                work_dir,
                container_id,
                "bash",
                "-lc",
                "secb patch",
            ],
            timeout=300.0,
        )
        if apply.returncode != 0:
            return (
                "secb patch failed inside regression container: "
                f"{(apply.stderr or apply.stdout or '').strip()}"
            )
        return None

    @staticmethod
    def _build_tree(container_id: str, *, work_dir: str) -> str | None:
        build = _docker(
            [
                "exec",
                "-w",
                work_dir,
                container_id,
                "bash",
                "-lc",
                "secb build",
            ],
            timeout=1800.0,
        )
        if build.returncode != 0:
            return (
                "secb build failed inside regression container: "
                f"{(build.stderr or build.stdout or '').strip()}"
            )
        return None
