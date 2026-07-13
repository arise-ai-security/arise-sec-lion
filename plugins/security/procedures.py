"""SEC-bench deterministic procedure executor for four mechanical roles.

Runs the fixed verifier, validator, and patch-application roles host-side with zero LLM
turns, driving the run's existing container session synchronously from Python (no
detached-nohup / poll dance). Verdicts and command evidence are Host-computed; the
official fresh-container evaluator remains the final target/base-identity authority.

On-disk artifacts (``exploit_validation_results.txt``, ``patch_validation_results.txt``,
``repro_run_*.log`` / ``fix_run_*.log`` and their ``.exit`` sentinels) keep the exact
names and format the prompt contract mandates, so downstream consumers (Reporter
prompts, ``criteria.py``, dashboards) are unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import stat
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from core.domain.values.procedure import ProcedureEvidence, ProcedurePlanApproval, ProcedureResult
from plugins.security.crash_signature import (
    CrashSignature,
    compute_crash_signature,
    signatures_match,
)
from plugins.security.cve_instance import CVEInstance
from plugins.security.deliverables import (
    ARTIFACT_PATHS,
    EXPLOIT_VALIDATION_FIELDS,
    PATCH_VALIDATION_FIELDS,
    PHASE_COMMANDS,
)
from plugins.security.patch_plan import PatchApplier, PatchPlan, PatchPlanValidator
from plugins.security.prompt_strategy import role_from_task


if TYPE_CHECKING:
    from collections.abc import Callable

    from core.ports.procedure_ports import ProcedureExecutorPort


# Per-command timeouts (seconds). settings.security carries no build/repro-specific
# knob today (only a generic worker docker timeout), so these are module constants.
_BUILD_TIMEOUT = 1800.0
_REPRO_TIMEOUT = 600.0
_PATCH_TIMEOUT = 600.0
_GIT_TIMEOUT = 30.0

_DIGEST_LIMIT = 4000
_EXCERPT_LIMIT = 500

# The seeded /testcase/repro.sh stub the runtime writes at prep and expects the
# Exploiter to replace (plugins/security/docker_runtime.py:_REPRO_SKELETON).
_REPRO_SKELETON_MARKER = "Arise seeded an empty"

# Static Tier-1 registry: role bracket -> procedure_ref.
_PROCEDURE_EXPLOIT = "secb_exploit_validation"
_PROCEDURE_PATCH = "secb_patch_validation"
_PROCEDURE_BUILD = "secb_build_validation"
_PROCEDURE_PATCH_APPLY = "secb_patch_apply"
_REGISTRY: dict[str, str] = {
    "Build-Verifier": _PROCEDURE_BUILD,
    "Exploit-Validator": _PROCEDURE_EXPLOIT,
    "Patch-Applier": _PROCEDURE_PATCH_APPLY,
    "Patch-Validator": _PROCEDURE_PATCH,
}
# The approved PatchPlan the mechanical Patch-Applier consumes (never reasons).
_PATCH_PLAN_JSON = PurePosixPath(ARTIFACT_PATHS["patch_plan"]).name
_PROCEDURE_REFS = frozenset(_REGISTRY.values())

# Canonical /testcase filenames, derived from the deliverables contract.
_REPRO_SH = PurePosixPath(ARTIFACT_PATHS["repro_script"]).name
_BASE_COMMIT_POINTER = PurePosixPath(ARTIFACT_PATHS["base_commit_hash"]).name
_BUILD_SCRIPT = PurePosixPath(ARTIFACT_PATHS["build_script"]).name
_REPO_CHANGES_DIFF = PurePosixPath(ARTIFACT_PATHS["repo_changes_diff"]).name
_POC_POINTER = PurePosixPath(ARTIFACT_PATHS["poc_path"]).name
_BINARY_POINTER = PurePosixPath(ARTIFACT_PATHS["binary_paths"]).name
_EXPLOIT_IDENTITY = PurePosixPath(ARTIFACT_PATHS["exploit_identity"]).name
_MODEL_PATCH = PurePosixPath(ARTIFACT_PATHS["model_patch"]).name
_EXPLOIT_VERDICT = PurePosixPath(ARTIFACT_PATHS["exploit_validation"]).name
_PATCH_VERDICT = PurePosixPath(ARTIFACT_PATHS["patch_validation"]).name
_REPRO_RUN_LOG = PurePosixPath(PHASE_COMMANDS["repro_loop"]["run_log"]).name
_REPRO_LOOP_EXIT = PurePosixPath(PHASE_COMMANDS["repro_loop"]["exit"]).name
_FIX_LOOP_LOG = PurePosixPath(PHASE_COMMANDS["fix_loop"]["log"]).name
_FIX_RUN_LOG = PurePosixPath(PHASE_COMMANDS["fix_loop"]["run_log"]).name
_FIX_LOOP_EXIT = PurePosixPath(PHASE_COMMANDS["fix_loop"]["exit"]).name

_DIFF_GIT_RX = re.compile(r"^diff --git a/\S+ b/(\S+)", re.MULTILINE)
_OBSERVED_ERROR_RX = re.compile(r"^OBSERVED_SANITIZER_ERROR:\s*(.+)$", re.MULTILINE)
_ASSERTION_FAILURE_RX = re.compile(
    r"\bassert(?:ion)?(?:\s+[^\n]+)?\s+failed\b", re.IGNORECASE
)
_UNSANITIZED_CRASH_RX = re.compile(
    r"\b(?:segmentation fault|segfault|bus error|illegal instruction|"
    r"floating point exception|core dumped|sig(?:segv|bus|ill|fpe|abrt)|aborted)\b",
    re.IGNORECASE,
)
_SANITIZER_MARKERS: tuple[tuple[str, str], ...] = (
    ("AddressSanitizer", "address-sanitizer"),
    ("LeakSanitizer", "leak-sanitizer"),
    ("ThreadSanitizer", "thread-sanitizer"),
    ("MemorySanitizer", "memory-sanitizer"),
    ("UndefinedBehaviorSanitizer", "undefined-behavior-sanitizer"),
    ("runtime error:", "undefined-behavior-sanitizer"),
)
_SANITIZER_PROBES: dict[str, tuple[str, str]] = {
    "address": ("ASAN_OPTIONS", "Available flags for AddressSanitizer:"),
    "memory": ("MSAN_OPTIONS", "Available flags for MemorySanitizer:"),
    "undefined": ("UBSAN_OPTIONS", "Available flags for UndefinedBehaviorSanitizer:"),
}
_SANITIZER_PROBE_SECONDS = 10

_REPLAY_CONTRACT_JSON = "replay-contract.json"
_PATCH_APPROVAL_JSON = "patch-approval.json"
_PATCH_TARGET_PREIMAGE = "patch-target-preimage"
_PROCEDURE_ATTEMPT_PARAM = "procedure_attempt"
_PROCEDURE_RESUME_PARAM = "procedure_resume"

_CANONICAL_REPRO_PREFIX = (
    "#!/bin/bash",
    "set -euo pipefail",
    'BIN="$(head -n 1 /testcase/binary_paths.txt)"',
    'POC="$(head -n 1 /testcase/poc_path.txt)"',
)
_REPRO_PLACEHOLDER_BINARY = "$BIN"
_REPRO_PLACEHOLDER_POC = "$POC"
_REPRO_REDIRECT_STDIN = "<"

_EMPTY_SIG = CrashSignature(None, None, None)


class ProcedureInfrastructureError(RuntimeError):
    """A procedure could not run for infrastructure reasons (no session, wiring bug).

    Distinct from task-level failure (a FAIL verdict), which is returned as
    ``ProcedureResult(success=False, ...)``. The dispatch caller converts this
    exception into a failed result and escalates to an agentic worker.
    """


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Result of one synchronous in-container command."""

    exit_code: int
    output: str
    timed_out: bool = False


class _ReplayContract(BaseModel):
    """Host-frozen identity of the pre-patch replay inputs and target."""

    model_config = {"frozen": True}

    schema_version: Literal["1"] = "1"
    instance_id: str
    base_commit: str
    sanitizer: str
    expected_exit_code: int
    work_dir: str
    source_head: str
    builder_baseline_sha256: str
    repro_sha256: str
    poc_path: str
    poc_sha256: str
    binary_paths: tuple[str, ...]
    selected_binary: str
    selected_binary_sha256: str
    binary_pointer_sha256: str
    replay_argv: tuple[str, ...]
    replay_stdin: bool
    exploit_verdict_sha256: str = ""

    @property
    def identity(self) -> str:
        payload = self.model_dump_json(exclude_none=False)
        return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class _PatchApproval(BaseModel):
    """Host-frozen link from a rendered patch to its replay identity."""

    model_config = {"frozen": True}

    schema_version: Literal["2"] = "2"
    replay_identity: str
    plan_sha256: str
    patch_sha256: str
    target_file: str
    preimage_sha256: str
    patched_sha256: str


class ProcedureSession(Protocol):
    """The run's container-session slice a procedure drives.

    The directories are host mirrors of the run's three worker-writable bind mounts.
    ``run`` executes an argv vector directly inside the run's container and returns
    its exit code + combined output; a timeout returns
    ``CommandOutcome(timed_out=True, ...)`` with the partial output rather than
    raising (a timeout is a task-level failure, not an infrastructure fault).
    """

    testcase_dir: Path
    source_dir: Path
    work_dir: Path
    identity_dir: Path

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome: ...


class SecBenchProcedureExecutor:
    """Match, validate, and execute the SEC-bench validator procedures.

    Implements :class:`core.ports.procedure_ports.ProcedureExecutorPort`.
    """

    def __init__(self, session_resolver: Callable[[UUID], ProcedureSession | None]) -> None:
        """Store the run's container-session resolver.

        Args:
            session_resolver: Maps a run's root agent id to its (shared) container
                session, or ``None`` when no session exists. Bootstrap wires this to
                the security plugin's per-run session registry; injected (not reached
                for) to keep the DIP boundary.
        """
        self._session_resolver = session_resolver

    def match(self, task_description: str, domain_context: object | None) -> str | None:
        """Return the procedure_ref for a task's leading ``[Role]`` bracket, or None."""
        _ = domain_context
        role = role_from_task(task_description)
        if role is None:
            return None
        return _REGISTRY.get(role.name)

    def resolve(self, procedure_ref: str) -> bool:
        """True iff a Host-matched ref names a registered procedure."""
        return procedure_ref in _PROCEDURE_REFS

    async def execute(
        self,
        procedure_ref: str,
        task_description: str,
        domain_context: object | None,
        params: dict[str, Any],
    ) -> ProcedureResult:
        """Run a validator procedure synchronously against the run's container.

        Task-level failure (FAIL verdict, preflight miss, command timeout) returns
        ``success=False`` with a bounded digest and never raises. ``params`` must
        carry ``root_id`` (the run's root agent UUID) so the container session can be
        resolved.

        Raises:
            ProcedureInfrastructureError: The run has no resolvable container session,
                ``root_id`` is missing from ``params``, or an unknown ref is dispatched.
        """
        _ = task_description
        cve = domain_context if isinstance(domain_context, CVEInstance) else None
        root_id = _root_id_from_params(params)
        session = self._session_resolver(root_id)
        if session is None:
            raise ProcedureInfrastructureError(f"no container session for run {root_id}")
        if procedure_ref == _PROCEDURE_BUILD:
            return await _run_build_validation(session, cve)
        if procedure_ref == _PROCEDURE_PATCH_APPLY:
            return await _run_patch_apply(session, cve)
        if procedure_ref == _PROCEDURE_EXPLOIT:
            return await _run_exploit_validation(session, cve)
        if procedure_ref == _PROCEDURE_PATCH:
            return await _run_patch_validation(
                session,
                cve,
                is_recheck=(
                    _procedure_attempt(params) > 1 or _procedure_resume(params)
                ),
            )
        raise ProcedureInfrastructureError(f"unknown procedure_ref {procedure_ref!r}")


# ---------------------------------------------------------------------------
# Build validation (deterministic Builder phase gate)
# ---------------------------------------------------------------------------


async def _run_build_validation(
    session: ProcedureSession, cve: CVEInstance | None
) -> ProcedureResult:
    """Host-determined Builder gate for real ELF binaries and sanitizer startup.

    The Host authors ``ProcedureExecutionFinished`` evidence instead of trusting an
    agent-authored verdict. The official fresh-container evaluator remains authoritative.
    """
    evidence: list[ProcedureEvidence] = []
    build_out = await _run_cmd(
        session,
        ("/usr/local/bin/secb", "build"),
        timeout=_BUILD_TIMEOUT,
        evidence=evidence,
    )
    if build_out.exit_code != 0 or build_out.timed_out:
        reason = _timeout_or(build_out, "secb build failed")
        return _failed(_PROCEDURE_BUILD, f"Build validation FAIL: {reason}", reason, evidence)
    binary_reason = await _validate_declared_binaries(session, cve, evidence)
    if binary_reason is not None:
        return _failed(
            _PROCEDURE_BUILD, f"Build validation FAIL: {binary_reason}", binary_reason, evidence
        )
    binary_count = len(_nonempty_lines(_read(session.testcase_dir / _BINARY_POINTER)))
    return ProcedureResult(
        success=True,
        summary=(
            "Build validation PASS: secb build succeeded and "
            f"{binary_count} declared ELF binary/binaries initialized the configured sanitizer"
        ),
        evidence=tuple(evidence),
    )


async def _validate_declared_binaries(
    session: ProcedureSession,
    cve: CVEInstance | None,
    evidence: list[ProcedureEvidence],
) -> str | None:
    if cve is None:
        return "CVE context is missing; configured sanitizer cannot be verified"
    probe = _SANITIZER_PROBES.get(cve.sanitizer.strip().lower())
    if probe is None:
        return f"unsupported configured sanitizer {cve.sanitizer!r}"

    paths = _nonempty_lines(_read(session.testcase_dir / _BINARY_POINTER))
    if not paths:
        return f"Builder binary pointer {_BINARY_POINTER} names no paths"
    environment, marker = probe
    for path in paths:
        if not PurePosixPath(path).is_absolute():
            return f"Builder binary path must be absolute: {path}"
        if not _is_rebuildable_binary_path(path):
            return f"Builder binary must remain at a rebuildable /src or /work path: {path}"
        binary = _resolve_workspace_file(session, path)
        structural_reason = _validate_host_binary(binary)
        evidence.append(_host_file_evidence("host-elf-check", path, structural_reason))
        if structural_reason is not None:
            return f"Builder binary {path} is not a non-empty executable ELF"
        runtime = await _run_cmd(
            session,
            (path, "--help"),
            timeout=float(_SANITIZER_PROBE_SECONDS),
            evidence=evidence,
            env=((environment, "help=1"),),
        )
        if runtime.timed_out or runtime.exit_code == 124:
            return f"Builder binary {path} sanitizer probe timed out"
        if marker not in runtime.output:
            return f"Builder binary {path} did not initialize the {cve.sanitizer} sanitizer runtime"
    return None


# ---------------------------------------------------------------------------
# Patch apply (weak literal Patch-Applier)
# ---------------------------------------------------------------------------


async def _run_patch_apply(
    session: ProcedureSession, cve: CVEInstance | None
) -> ProcedureResult:
    """Apply the reasoning worker's PatchPlan literally, or block without editing.

    The Root-Cause-Analyst authors ``patch_plan.json``; this applier NEVER reasons. It
    validates the frozen plan against the sealed ``/src`` source (hash, unique
    anchor, allow/forbid paths) and either applies it exactly and emits the diff
    deliverable, or returns ``PATCH_PLAN_BLOCKED`` leaving every byte untouched.
    """
    evidence: list[ProcedureEvidence] = []
    tc = session.testcase_dir
    _clear_patch_approval(session)
    plan_text = _read(tc / _PATCH_PLAN_JSON)
    if not plan_text.strip():
        reason = f"{_PATCH_PLAN_JSON} missing or empty — reasoning worker authored no PatchPlan"
        return _failed(_PROCEDURE_PATCH_APPLY, f"Patch apply BLOCKED: {reason}", reason, evidence)
    try:
        plan = PatchPlan.model_validate_json(plan_text)
    except ValidationError as exc:
        reason = f"PatchPlan schema invalid ({exc.error_count()} errors)"
        return _failed(_PROCEDURE_PATCH_APPLY, f"Patch apply BLOCKED: {reason}", reason, evidence)

    replay_contract, replay_reason = await _validate_replay_contract(session, cve)
    if replay_contract is None:
        reason = replay_reason or "pre-patch replay identity is unavailable"
        return _failed(_PROCEDURE_PATCH_APPLY, f"Patch apply BLOCKED: {reason}", reason, evidence)
    if plan.pre_patch_exploit_identity != replay_contract.identity:
        reason = (
            "PatchPlan pre_patch_exploit_identity does not match the Host-frozen "
            f"identity {replay_contract.identity}"
        )
        return _failed(_PROCEDURE_PATCH_APPLY, f"Patch apply BLOCKED: {reason}", reason, evidence)

    resolved_evidence = _resolved_plan_evidence(tc, plan.evidence_references)
    applier = PatchApplier(PatchPlanValidator(session.source_dir, resolved_evidence))
    outcome = applier.render(plan)
    evidence.append(
        ProcedureEvidence(
            argv=("patch-plan-render", plan.target_file),
            exit_code=0 if outcome.status == "RENDERED" else 1,
            output_sha256=outcome.plan_sha256,
            excerpt=outcome.reason[:_EXCERPT_LIMIT],
        )
    )
    if outcome.status != "RENDERED":
        return _failed(
            _PROCEDURE_PATCH_APPLY,
            f"Patch apply BLOCKED: {outcome.reason}",
            outcome.reason,
            evidence,
        )
    if outcome.original_content is None or outcome.patched_content is None:
        raise ProcedureInfrastructureError(
            "rendered PatchPlan omitted the frozen target transformation"
        )

    _write_file(tc / _MODEL_PATCH, outcome.diff)
    _write_private_bytes(
        _patch_target_preimage_path(session),
        outcome.original_content.encode("utf-8"),
    )
    _freeze_patch_approval(
        session,
        _PatchApproval(
            replay_identity=replay_contract.identity,
            plan_sha256=outcome.plan_sha256,
            patch_sha256=_sha256_file(tc / _MODEL_PATCH),
            target_file=plan.target_file,
            preimage_sha256=hashlib.sha256(
                outcome.original_content.encode("utf-8")
            ).hexdigest(),
            patched_sha256=hashlib.sha256(
                outcome.patched_content.encode("utf-8")
            ).hexdigest(),
        ),
    )
    return ProcedureResult(
        success=True,
        summary=(
            f"Patch apply RENDERED plan {outcome.plan_sha256[:12]} for {plan.target_file}; "
            f"bound to {replay_contract.identity}"
        ),
        evidence=tuple(evidence),
        plan_approval=ProcedurePlanApproval(
            plan_sha256=outcome.plan_sha256,
            evidence_references=plan.evidence_references,
        ),
    )


def _resolved_plan_evidence(testcase: Path, references: tuple[str, ...]) -> set[str]:
    """Resolve non-empty plan evidence under the sealed testcase directory."""
    resolved: set[str] = set()
    for reference in references:
        candidate = _resolve_testcase_file(testcase, reference)
        if candidate is not None and _regular_file_size(candidate) > 0:
            resolved.add(reference)
    return resolved


# ---------------------------------------------------------------------------
# Host-frozen replay and patch identity
# ---------------------------------------------------------------------------


async def _capture_replay_contract(
    session: ProcedureSession,
    cve: CVEInstance | None,
    *,
    include_verdict: bool = False,
    frozen_binary_sha256: str | None = None,
) -> tuple[_ReplayContract | None, str | None]:
    """Build the current replay identity after enforcing its artifact handoff."""
    if cve is None:
        return None, "CVE context is missing; replay target identity cannot be frozen"

    tc = session.testcase_dir
    repro = _read(tc / _REPRO_SH)
    if not repro.strip():
        return None, f"{ARTIFACT_PATHS['repro_script']} missing or empty"
    if _REPRO_SKELETON_MARKER in repro:
        return None, f"{ARTIFACT_PATHS['repro_script']} is still the seeded skeleton (not replaced)"
    replay_template = _canonical_replay_template(repro)
    if replay_template is None:
        return None, (
            "repro.sh is not a Host-verifiable direct replay; use the canonical "
            "binary_paths.txt/poc_path.txt exec form with inert arguments"
        )

    work_dir = PurePosixPath(cve.work_dir)
    if not work_dir.is_absolute():
        return None, f"CVE work_dir must be absolute: {cve.work_dir}"

    poc_path = _first_line(_read(tc / _POC_POINTER))
    if not poc_path:
        return None, f"PoC pointer {_POC_POINTER} names no path"
    poc_file = _resolve_testcase_file(tc, poc_path)
    if poc_file is None:
        return None, f"PoC path must resolve to a regular file under /testcase: {poc_path}"

    binary_paths = tuple(_nonempty_lines(_read(tc / _BINARY_POINTER)))
    if not binary_paths:
        return None, f"Builder binary pointer {_BINARY_POINTER} names no paths"
    if any(not PurePosixPath(path).is_absolute() for path in binary_paths):
        return None, "every Builder binary path must be absolute"
    if any(not _is_rebuildable_binary_path(path) for path in binary_paths):
        return None, "every Builder binary must remain at a rebuildable /src or /work path"

    selected_binary = binary_paths[0]
    selected_binary_file: Path | None = None
    selected_binary_sha256 = frozen_binary_sha256
    if selected_binary_sha256 is None:
        selected_binary_file = _resolve_workspace_file(session, selected_binary)
        if selected_binary_file is None:
            return None, (
                f"selected binary does not resolve inside the run workspace: {selected_binary}"
            )
        if _validate_host_binary(selected_binary_file) is not None:
            return None, f"selected binary is not a non-empty executable ELF: {selected_binary}"
        try:
            selected_binary_sha256 = _sha256_file(selected_binary_file)
        except OSError:
            return None, "selected binary must be a stable regular file"

    source_head, builder_baseline, baseline_reason = await asyncio.to_thread(
        _measure_builder_baseline, session, cve
    )
    if baseline_reason is not None:
        return None, baseline_reason

    replay_args, replay_stdin = replay_template
    replay_argv = (selected_binary,) + tuple(
        poc_path if argument == _REPRO_PLACEHOLDER_POC else argument
        for argument in replay_args
    )

    verdict_hash = ""
    if include_verdict:
        verdict = tc / _EXPLOIT_VERDICT
        try:
            verdict_bytes = _read_regular_bytes(verdict)
        except OSError:
            verdict_bytes = b""
        if not verdict_bytes:
            return None, f"{ARTIFACT_PATHS['exploit_validation']} missing or empty"
        verdict_hash = hashlib.sha256(verdict_bytes).hexdigest()

    try:
        contract = _ReplayContract(
            instance_id=cve.instance_id,
            base_commit=cve.base_commit,
            sanitizer=cve.sanitizer,
            expected_exit_code=cve.exit_code,
            work_dir=cve.work_dir,
            source_head=source_head,
            builder_baseline_sha256=builder_baseline,
            repro_sha256=_sha256_file(tc / _REPRO_SH),
            poc_path=poc_path,
            poc_sha256=_sha256_file(poc_file),
            binary_paths=binary_paths,
            selected_binary=selected_binary,
            selected_binary_sha256=selected_binary_sha256,
            binary_pointer_sha256=_sha256_file(tc / _BINARY_POINTER),
            replay_argv=replay_argv,
            replay_stdin=replay_stdin,
            exploit_verdict_sha256=verdict_hash,
        )
    except OSError:
        return None, "replay inputs must be stable regular files inside the run workspace"
    return contract, None


def _resolve_testcase_file(testcase: Path, container_path: str) -> Path | None:
    return _resolve_mounted_file(testcase, container_path, "/testcase")


def _resolve_workspace_file(session: ProcedureSession, container_path: str) -> Path | None:
    for mount, root in (
        ("/src", session.source_dir),
        ("/testcase", session.testcase_dir),
        ("/work", session.work_dir),
    ):
        candidate = _resolve_mounted_file(root, container_path, mount)
        if candidate is not None:
            return candidate
    return None


def _resolve_rebuild_output(session: ProcedureSession, container_path: str) -> Path | None:
    for mount, root in (("/src", session.source_dir), ("/work", session.work_dir)):
        relative = _mounted_relative_parts(container_path, mount)
        if not relative:
            continue
        candidate = root.joinpath(*relative)
        if _safe_mounted_path(root, candidate.parent, require_directory=True):
            return candidate
    return None


def _is_rebuildable_binary_path(container_path: str) -> bool:
    parts = PurePosixPath(container_path).parts
    return parts[:2] in (("/", "src"), ("/", "work"))


def _resolve_workspace_directory(
    session: ProcedureSession, container_path: str
) -> Path | None:
    for mount, root in (
        ("/src", session.source_dir),
        ("/testcase", session.testcase_dir),
        ("/work", session.work_dir),
    ):
        relative = _mounted_relative_parts(container_path, mount)
        if relative is None:
            continue
        candidate = root.joinpath(*relative)
        if _safe_mounted_path(root, candidate, require_directory=True):
            return candidate
    return None


def _resolve_mounted_file(
    root: Path, container_path: str, container_mount: str
) -> Path | None:
    relative = _mounted_relative_parts(container_path, container_mount)
    if not relative:
        return None
    candidate = root.joinpath(*relative)
    if not _safe_mounted_path(root, candidate, require_directory=False):
        return None
    return candidate


def _mounted_relative_parts(
    container_path: str, container_mount: str
) -> tuple[str, ...] | None:
    path = PurePosixPath(container_path)
    mount = PurePosixPath(container_mount)
    if not path.is_absolute() or ".." in path.parts:
        return None
    mount_parts = mount.parts
    if path.parts[: len(mount_parts)] != mount_parts:
        return None
    return tuple(path.parts[len(mount_parts) :])


def _safe_mounted_path(root: Path, candidate: Path, *, require_directory: bool) -> bool:
    try:
        root_resolved = root.resolve(strict=True)
        relative = candidate.relative_to(root)
    except (OSError, ValueError):
        return False
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return False
        is_final = index == len(relative.parts) - 1
        if not is_final and not stat.S_ISDIR(mode):
            return False
    try:
        resolved = candidate.resolve(strict=True)
        mode = candidate.lstat().st_mode
    except OSError:
        return False
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return False
    return stat.S_ISDIR(mode) if require_directory else stat.S_ISREG(mode)


def _validate_host_binary(path: Path | None) -> str | None:
    if path is None:
        return "binary path is outside the mounted workspace"
    try:
        mode = path.lstat().st_mode
        content = _read_regular_bytes(path)
    except OSError:
        return "binary is not a stable regular file"
    if not stat.S_ISREG(mode) or not mode & 0o111:
        return "binary is not executable"
    if len(content) < 4 or content[:4] != b"\x7fELF":
        return "binary does not have an ELF header"
    return None


def _host_file_evidence(
    operation: str, container_path: str, failure: str | None
) -> ProcedureEvidence:
    output = failure or "regular executable ELF"
    return ProcedureEvidence(
        argv=(operation, container_path),
        exit_code=1 if failure else 0,
        output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        excerpt=output,
    )


def _measure_builder_baseline(
    session: ProcedureSession, cve: CVEInstance
) -> tuple[str, str, str | None]:
    try:
        base_pointer = _read_regular_bytes(
            session.testcase_dir / _BASE_COMMIT_POINTER
        ).decode("utf-8", errors="replace").strip()
        repo_changes = _read_regular_bytes(session.testcase_dir / _REPO_CHANGES_DIFF)
        build_script = _read_regular_bytes(session.source_dir / _BUILD_SCRIPT)
    except OSError:
        return "", "", "Builder baseline artifacts are missing or are not regular files"
    if base_pointer != cve.base_commit:
        return "", "", "Builder base-commit artifact does not match the CVE base commit"

    worktree = _resolve_workspace_directory(session, cve.work_dir)
    if worktree is None:
        return "", "", f"CVE work_dir does not resolve inside the workspace: {cve.work_dir}"

    source_head = base_pointer
    tracked_state = b""
    if PurePosixPath(cve.work_dir).parts[:2] == ("/", "src"):
        source_head, tracked_state, reason = _measure_git_state(
            worktree, cve.base_commit, repo_changes
        )
        if reason is not None:
            return "", "", reason

    digest = hashlib.sha256()
    for label, value in (
        (b"source-head", source_head.encode("ascii", errors="replace")),
        (b"tracked-state", tracked_state),
        (b"build-script", build_script),
        (b"repo-changes", repo_changes),
    ):
        digest.update(label + b"\0" + str(len(value)).encode("ascii") + b"\0" + value)
    return source_head, digest.hexdigest(), None


def _measure_git_state(
    worktree: Path, base_commit: str, repo_changes: bytes
) -> tuple[str, bytes, str | None]:
    try:
        head = _git_output(worktree, "rev-parse", "HEAD").decode("ascii").strip()
        _git_output(worktree, "cat-file", "-e", f"{base_commit}^{{commit}}")
        _git_output(worktree, "merge-base", "--is-ancestor", base_commit, head)
        worktree_diff = _git_output(
            worktree,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        )
        baseline_diff = _git_output(
            worktree,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            base_commit,
            head,
            "--",
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        return "", b"", f"Builder git baseline could not be measured: {exc}"
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
        return "", b"", "Builder git HEAD is not a full commit hash"
    if worktree_diff:
        return "", b"", "tracked source or build inputs changed after the Builder baseline"
    if baseline_diff != repo_changes:
        return "", b"", "Builder commit does not match repo_changes.diff"
    return head, baseline_diff, None


def _git_output(worktree: Path, *arguments: str) -> bytes:
    command = (
        "git",
        "-c",
        f"safe.directory={worktree}",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.untrackedCache=false",
        *arguments,
    )
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
    }
    completed = subprocess.run(
        command,
        cwd=worktree,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise subprocess.CalledProcessError(
            completed.returncode, command, output=completed.stdout, stderr=detail
        )
    return completed.stdout


def _canonical_replay_template(repro: str) -> tuple[tuple[str, ...], bool] | None:
    lines = tuple(line.strip() for line in repro.splitlines() if line.strip())
    if len(lines) != len(_CANONICAL_REPRO_PREFIX) + 1:
        return None
    if lines[:-1] != _CANONICAL_REPRO_PREFIX:
        return None
    try:
        tokens = tuple(shlex.split(lines[-1], posix=True))
    except ValueError:
        return None
    if len(tokens) < 3 or tokens[:2] != ("exec", _REPRO_PLACEHOLDER_BINARY):
        return None
    arguments = tokens[2:]
    if arguments[-2:] == (_REPRO_REDIRECT_STDIN, _REPRO_PLACEHOLDER_POC):
        replay_stdin = True
        arguments = arguments[:-2]
        if _REPRO_PLACEHOLDER_POC in arguments:
            return None
    else:
        replay_stdin = False
        if arguments.count(_REPRO_PLACEHOLDER_POC) != 1:
            return None
    if any(_unsafe_replay_argument(argument) for argument in arguments):
        return None
    return arguments, replay_stdin


def _unsafe_replay_argument(argument: str) -> bool:
    if any(token in argument for token in (";", "|", "&", ">", "<")):
        return True
    if argument != _REPRO_PLACEHOLDER_POC and ("$" in argument or "`" in argument):
        return True
    return not argument or any(ord(character) < 32 for character in argument)


def _replay_stdin_bytes(
    session: ProcedureSession, contract: _ReplayContract
) -> tuple[bytes | None, str | None]:
    if not contract.replay_stdin:
        return None, None
    poc = _resolve_testcase_file(session.testcase_dir, contract.poc_path)
    if poc is None:
        return None, "frozen PoC no longer resolves to a regular file under /testcase"
    try:
        content = _read_regular_bytes(poc)
    except OSError:
        return None, "frozen PoC could not be read safely"
    if hashlib.sha256(content).hexdigest() != contract.poc_sha256:
        return None, "frozen PoC changed before direct replay"
    return content, None


def _replay_environment(sanitizer: str) -> tuple[tuple[str, str], ...]:
    normalized = sanitizer.strip().lower()
    if normalized == "address":
        return (("ASAN_OPTIONS", "abort_on_error=1:halt_on_error=1"),)
    if normalized == "memory":
        return (("MSAN_OPTIONS", "halt_on_error=1"),)
    if normalized == "undefined":
        return (("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1"),)
    return ()


def _freeze_replay_contract(session: ProcedureSession, contract: _ReplayContract) -> None:
    _write_private_json(_replay_contract_path(session), contract.model_dump_json())
    _write_file(session.testcase_dir / _EXPLOIT_IDENTITY, contract.identity + "\n")


async def _validate_replay_contract(
    session: ProcedureSession, cve: CVEInstance | None
) -> tuple[_ReplayContract | None, str | None]:
    stored, load_reason = _load_replay_contract(session)
    if stored is None:
        return None, load_reason
    current, capture_reason = await _capture_replay_contract(
        session, cve, include_verdict=True
    )
    if current is None:
        return None, capture_reason
    if current != stored:
        return None, "protected replay inputs or target identity changed after exploit validation"
    public_identity = _read(session.testcase_dir / _EXPLOIT_IDENTITY).strip()
    if public_identity != stored.identity:
        return None, "public exploit input identity does not match the Host-frozen identity"
    return stored, None


def _load_replay_contract(
    session: ProcedureSession,
) -> tuple[_ReplayContract | None, str | None]:
    path = _replay_contract_path(session)
    try:
        payload = _read_regular_bytes(path)
        return _ReplayContract.model_validate_json(payload), None
    except FileNotFoundError:
        return None, "Host-frozen pre-patch replay identity is missing"
    except (OSError, ValidationError) as exc:
        return None, f"Host-frozen pre-patch replay identity is invalid: {exc}"


def _clear_replay_contract(session: ProcedureSession) -> None:
    (session.testcase_dir / _EXPLOIT_IDENTITY).unlink(missing_ok=True)
    _replay_contract_path(session).unlink(missing_ok=True)
    _clear_patch_approval(session)


def _freeze_patch_approval(session: ProcedureSession, approval: _PatchApproval) -> None:
    _write_private_json(_patch_approval_path(session), approval.model_dump_json())


def _validate_patch_approval(
    session: ProcedureSession,
    replay_contract: _ReplayContract,
    patch_text: str,
) -> tuple[_PatchApproval | None, str | None]:
    path = _patch_approval_path(session)
    try:
        approval = _PatchApproval.model_validate_json(_read_regular_bytes(path))
    except FileNotFoundError:
        return None, "Host patch approval is missing; model_patch.diff is not trusted"
    except (OSError, ValidationError) as exc:
        return None, f"Host patch approval is invalid: {exc}"
    if approval.replay_identity != replay_contract.identity:
        return None, "Host patch approval targets a different pre-patch replay identity"
    if approval.patch_sha256 != hashlib.sha256(patch_text.encode("utf-8")).hexdigest():
        return None, "model_patch.diff changed after Host approval"
    try:
        plan = PatchPlan.model_validate_json(_read(session.testcase_dir / _PATCH_PLAN_JSON))
    except ValidationError:
        return None, "patch_plan.json changed or became invalid after Host approval"
    if plan.sha256 != approval.plan_sha256:
        return None, "patch_plan.json changed after Host approval"
    if plan.target_file != approval.target_file:
        return None, "PatchPlan target changed after Host approval"
    try:
        preimage = _read_regular_bytes(_patch_target_preimage_path(session))
    except OSError as exc:
        return None, f"Host patch target preimage is unavailable: {exc}"
    if hashlib.sha256(preimage).hexdigest() != approval.preimage_sha256:
        return None, "Host patch target preimage changed after approval"
    return approval, None


def _clear_patch_approval(session: ProcedureSession) -> None:
    _patch_approval_path(session).unlink(missing_ok=True)
    _patch_target_preimage_path(session).unlink(missing_ok=True)


def _replay_contract_path(session: ProcedureSession) -> Path:
    return session.identity_dir / _REPLAY_CONTRACT_JSON


def _patch_approval_path(session: ProcedureSession) -> Path:
    return session.identity_dir / _PATCH_APPROVAL_JSON


def _patch_target_preimage_path(session: ProcedureSession) -> Path:
    return session.identity_dir / _PATCH_TARGET_PREIMAGE


def _write_private_json(path: Path, payload: str) -> None:
    _atomic_write(path, payload, mode=0o600)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    _atomic_write_bytes(path, payload, mode=0o600)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_regular_bytes(path)).hexdigest()


# ---------------------------------------------------------------------------
# Exploit validation
# ---------------------------------------------------------------------------


async def _run_exploit_validation(
    session: ProcedureSession, cve: CVEInstance | None
) -> ProcedureResult:
    evidence: list[ProcedureEvidence] = []
    tc = session.testcase_dir
    golden = compute_crash_signature(cve.sanitizer_report) if cve is not None else _EMPTY_SIG
    _clear_replay_contract(session)

    replay_contract, preflight_reason = await _exploit_preflight(session, cve, evidence)
    if replay_contract is None:
        reason = preflight_reason or "replay input contract is unavailable"
        _write_exploit_verdict(tc, "FAIL", reason, golden, _EMPTY_SIG, 0)
        summary = f"Exploit validation FAIL: {reason}"
        return _failed(_PROCEDURE_EXPLOIT, summary, reason, evidence)

    _clear_logs(tc, "repro_run_*.log")
    run_sigs: list[CrashSignature] = []
    run_outcomes: list[CommandOutcome] = []
    replay_stdin, stdin_reason = _replay_stdin_bytes(session, replay_contract)
    if stdin_reason is not None:
        _write_exploit_verdict(tc, "FAIL", stdin_reason, golden, _EMPTY_SIG, 0)
        return _failed(
            _PROCEDURE_EXPLOIT,
            f"Exploit validation FAIL: {stdin_reason}",
            stdin_reason,
            evidence,
        )
    for index in (1, 2, 3):
        outcome = await _run_cmd(
            session,
            replay_contract.replay_argv,
            timeout=_REPRO_TIMEOUT,
            evidence=evidence,
            stdin=replay_stdin,
            env=_replay_environment(replay_contract.sanitizer),
        )
        _write_run_log(tc, _REPRO_RUN_LOG.replace("$N", str(index)), outcome)
        if outcome.timed_out:
            reason = f"secb repro timed out on run {index}"
            _write_exploit_verdict(tc, "FAIL", reason, golden, _EMPTY_SIG, index - 1)
            summary = f"Exploit validation FAIL: {reason}"
            return _failed(_PROCEDURE_EXPLOIT, summary, reason, evidence)
        run_sigs.append(compute_crash_signature(outcome.output))
        run_outcomes.append(outcome)
    _write_sentinel(tc / _REPRO_LOOP_EXIT)

    observed, determinism = _consensus(run_sigs)
    matched = signatures_match(observed, golden)
    termination_consistent = _terminations_consistent(run_outcomes)
    passed = matched and determinism == 3 and termination_consistent
    reason = _exploit_reason(
        passed,
        matched,
        determinism,
        termination_consistent,
        observed,
        golden,
    )
    verdict = "PASS" if passed else "FAIL"
    _write_exploit_verdict(tc, verdict, reason, golden, observed, determinism)
    summary = f"Exploit validation {verdict}: {reason}"
    if passed:
        frozen = replay_contract.model_copy(
            update={"exploit_verdict_sha256": _sha256_file(tc / _EXPLOIT_VERDICT)}
        )
        _freeze_replay_contract(session, frozen)
        return ProcedureResult(
            success=True,
            summary=f"{summary}; input identity {frozen.identity}",
            evidence=tuple(evidence),
        )
    return _failed(_PROCEDURE_EXPLOIT, summary, reason, evidence)


async def _exploit_preflight(
    session: ProcedureSession,
    cve: CVEInstance | None,
    evidence: list[ProcedureEvidence],
) -> tuple[_ReplayContract | None, str | None]:
    """Return the replay contract and no reason, or a fail-closed reason."""
    replay_contract, contract_reason = await _capture_replay_contract(session, cve)
    if replay_contract is None:
        return None, contract_reason
    binary_reason = await _validate_declared_binaries(session, cve, evidence)
    if binary_reason is not None:
        return None, binary_reason
    return replay_contract, None


def _exploit_reason(
    passed: bool,
    matched: bool,
    determinism: int,
    termination_consistent: bool,
    observed: CrashSignature,
    golden: CrashSignature,
) -> str:
    if passed:
        return (
            f"observed {observed.sanitizer_class}/{observed.top_frame} matches the CVE "
            f"oracle across 3/3 deterministic runs"
        )
    if determinism < 3:
        return f"crash is not deterministic ({determinism}/3 runs share the top signature)"
    if not termination_consistent:
        return "repro exit codes/signals are inconsistent across the three runs"
    if not matched:
        expected = f"{golden.sanitizer_class or 'unknown'}/{golden.top_frame or 'unknown'}"
        seen = f"{observed.sanitizer_class or 'none'}/{observed.top_frame or 'none'}"
        return f"observed {seen} does not match expected {expected}"
    return "verdict conditions not met"


def _write_exploit_verdict(
    tc: Path,
    verdict: str,
    reason: str,
    golden: CrashSignature,
    observed: CrashSignature,
    determinism: int,
) -> None:
    values = {
        "VERDICT": verdict,
        "REASON": reason,
        "EXPECTED_SANITIZER_ERROR": golden.sanitizer_class or "unknown",
        "OBSERVED_SANITIZER_ERROR": observed.sanitizer_class or "none",
        "CRASH_FUNCTION_EXPECTED": golden.top_frame or "unknown",
        "CRASH_FUNCTION_OBSERVED": observed.top_frame or "unknown",
        "DETERMINISM_RUNS": f"{determinism}/3",
        "CORRUPTION_ORIGIN_FUNCTION": "unknown",
    }
    _write_verdict_file(tc / _EXPLOIT_VERDICT, EXPLOIT_VALIDATION_FIELDS, values)


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------


async def _run_patch_validation(
    session: ProcedureSession,
    cve: CVEInstance | None,
    *,
    is_recheck: bool,
) -> ProcedureResult:
    evidence: list[ProcedureEvidence] = []
    tc = session.testcase_dir
    patch_text = _read(tc / _MODEL_PATCH)
    pre = _pre_patch_error(tc)
    patched_files = _patched_files(patch_text)

    if not patch_text.strip():
        reason = f"{ARTIFACT_PATHS['model_patch']} is missing or empty"
        _write_patch_verdict(
            tc, "FAIL", reason, pre, "unknown", "failed", "failed", 0, patched_files
        )
        return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)

    if is_recheck:
        replay_contract, replay_reason = await _prepare_patch_recheck(
            session, cve, patch_text
        )
    else:
        replay_contract, replay_reason = await _validate_replay_contract(session, cve)
    if replay_contract is None:
        reason = replay_reason or "pre-patch replay identity is unavailable"
        _write_patch_verdict(
            tc, "FAIL", reason, pre, "unknown", "failed", "failed", 0, patched_files
        )
        return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)
    _approval, approval_reason = _validate_patch_approval(
        session, replay_contract, patch_text
    )
    if approval_reason is not None:
        _write_patch_verdict(
            tc,
            "FAIL",
            approval_reason,
            pre,
            "unknown",
            "failed",
            "failed",
            0,
            patched_files,
        )
        return _failed(
            _PROCEDURE_PATCH,
            f"Patch validation FAIL: {approval_reason}",
            approval_reason,
            evidence,
        )

    reset_reason = (
        _reset_recheck_build_state(
            session,
            cve,
            replay_contract.binary_paths,
        )
        if is_recheck
        else None
    )
    if reset_reason is not None:
        _write_patch_verdict(
            tc,
            "FAIL",
            reset_reason,
            pre,
            "unknown",
            "failed",
            "failed",
            0,
            patched_files,
        )
        return _failed(
            _PROCEDURE_PATCH,
            f"Patch validation FAIL: {reset_reason}",
            reset_reason,
            evidence,
        )

    patch_out = await _run_cmd(
        session,
        ("/usr/local/bin/secb", "patch"),
        timeout=_PATCH_TIMEOUT,
        evidence=evidence,
    )
    apply_status = _apply_status(patch_out)
    fix_log = [patch_out.output, f"patch_exit={patch_out.exit_code}"]
    if apply_status != "clean":
        _write_file(tc / _FIX_LOOP_LOG, _join(fix_log))
        _write_sentinel(tc / _FIX_LOOP_EXIT)
        reason = _timeout_or(patch_out, "secb patch did not apply cleanly")
        _write_patch_verdict(
            tc, "FAIL", reason, pre, "unknown", apply_status, "failed", 0, patched_files
        )
        return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)

    build_out = await _run_cmd(
        session,
        ("/usr/local/bin/secb", "build"),
        timeout=_BUILD_TIMEOUT,
        evidence=evidence,
    )
    build_status = "success" if build_out.exit_code == 0 and not build_out.timed_out else "failed"
    fix_log += [build_out.output, f"build_exit={build_out.exit_code}"]
    _write_file(tc / _FIX_LOOP_LOG, _join(fix_log))
    if build_status != "success":
        _write_sentinel(tc / _FIX_LOOP_EXIT)
        reason = _timeout_or(build_out, "secb build failed after applying the patch")
        _write_patch_verdict(
            tc, "FAIL", reason, pre, "unknown", apply_status, build_status, 0, patched_files
        )
        return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)

    binary_reason = await _validate_declared_binaries(session, cve, evidence)
    if binary_reason is not None:
        _write_sentinel(tc / _FIX_LOOP_EXIT)
        reason = f"post-patch binary validation failed: {binary_reason}"
        _write_patch_verdict(
            tc, "FAIL", reason, pre, "unknown", apply_status, "failed", 0, patched_files
        )
        return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)

    return await _patch_repro_loop(
        session,
        evidence,
        replay_contract,
        _PatchContext(tc=tc, pre=pre, apply_status=apply_status, patched_files=patched_files),
    )


async def _prepare_patch_recheck(
    session: ProcedureSession,
    cve: CVEInstance | None,
    patch_text: str,
) -> tuple[_ReplayContract | None, str | None]:
    """Restore the approved pre-patch target before a stateful Host recheck."""
    stored, load_reason = _load_replay_contract(session)
    if stored is None:
        return None, load_reason
    approval, approval_reason = _validate_patch_approval(session, stored, patch_text)
    if approval is None:
        return None, approval_reason

    target = _resolve_workspace_file(session, approval.target_file)
    if target is None:
        return None, "Host-approved patch target no longer resolves inside /src"
    try:
        current = _read_regular_bytes(target)
        preimage = _read_regular_bytes(_patch_target_preimage_path(session))
        target_mode = stat.S_IMODE(target.lstat().st_mode)
    except OSError as exc:
        return None, f"Host-approved patch target cannot be read safely: {exc}"

    current_sha256 = hashlib.sha256(current).hexdigest()
    if current_sha256 not in {approval.preimage_sha256, approval.patched_sha256}:
        return None, "patch target drifted outside the Host-approved pre/post images"
    if current_sha256 == approval.patched_sha256:
        _atomic_write_bytes(target, preimage, mode=target_mode)

    current_contract, capture_reason = await _capture_replay_contract(
        session,
        cve,
        include_verdict=True,
        frozen_binary_sha256=stored.selected_binary_sha256,
    )
    if current_contract is None:
        return None, capture_reason
    if current_contract != stored:
        return None, "protected replay inputs changed before the Host recheck"
    public_identity = _read(session.testcase_dir / _EXPLOIT_IDENTITY).strip()
    if public_identity != stored.identity:
        return None, "public exploit input identity does not match the Host-frozen identity"
    return stored, None


def _reset_recheck_build_state(
    session: ProcedureSession,
    cve: CVEInstance | None,
    binary_paths: tuple[str, ...],
) -> str | None:
    """Remove mutable repair artifacts so the Host build must recreate outputs."""
    if cve is not None and PurePosixPath(cve.work_dir).parts[:2] == ("/", "src"):
        worktree = _resolve_workspace_directory(session, cve.work_dir)
        if worktree is None:
            return f"CVE work_dir does not resolve inside the workspace: {cve.work_dir}"
        try:
            _git_output(worktree, "clean", "-fdx", "--")
        except (OSError, subprocess.SubprocessError) as exc:
            return f"untracked build inputs could not be reset before Host recheck: {exc}"

    for container_path in binary_paths:
        output = _resolve_rebuild_output(session, container_path)
        if output is None:
            return f"declared build output cannot be reset safely: {container_path}"
        try:
            mode = output.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"declared build output cannot be inspected safely: {container_path}: {exc}"
        if stat.S_ISDIR(mode):
            return f"declared build output is a directory: {container_path}"
        try:
            output.unlink()
        except OSError as exc:
            return f"declared build output cannot be removed safely: {container_path}: {exc}"
    return None


@dataclass(frozen=True, slots=True)
class _PatchContext:
    """Carried state for the post-build repro loop (keeps the arg list small)."""

    tc: Path
    pre: str
    apply_status: str
    patched_files: str


async def _patch_repro_loop(
    session: ProcedureSession,
    evidence: list[ProcedureEvidence],
    replay_contract: _ReplayContract,
    ctx: _PatchContext,
) -> ProcedureResult:
    tc = ctx.tc
    _clear_logs(tc, "fix_run_*.log")
    failures: list[tuple[str, str]] = []
    clean_runs = 0
    replay_stdin, stdin_reason = _replay_stdin_bytes(session, replay_contract)
    if stdin_reason is not None:
        _write_sentinel(tc / _FIX_LOOP_EXIT)
        _write_patch_verdict(
            tc,
            "FAIL",
            stdin_reason,
            ctx.pre,
            "unknown",
            ctx.apply_status,
            "success",
            0,
            ctx.patched_files,
        )
        return _failed(
            _PROCEDURE_PATCH,
            f"Patch validation FAIL: {stdin_reason}",
            stdin_reason,
            evidence,
        )
    for index in (1, 2, 3):
        outcome = await _run_cmd(
            session,
            replay_contract.replay_argv,
            timeout=_REPRO_TIMEOUT,
            evidence=evidence,
            stdin=replay_stdin,
            env=_replay_environment(replay_contract.sanitizer),
        )
        _write_run_log(tc, _FIX_RUN_LOG.replace("$N", str(index)), outcome)
        if outcome.timed_out:
            _write_sentinel(tc / _FIX_LOOP_EXIT)
            reason = f"secb repro timed out on post-patch run {index}"
            _write_patch_verdict(
                tc, "FAIL", reason, ctx.pre, "unknown", ctx.apply_status, "success",
                clean_runs, ctx.patched_files,
            )
            return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)
        signature = compute_crash_signature(outcome.output)
        failure = _post_patch_run_failure(
            outcome,
            signature,
            expected_exit_code=replay_contract.expected_exit_code,
        )
        if failure is None:
            clean_runs += 1
            continue
        reason, sanitizer_error = failure
        failures.append((f"post-patch run {index} {reason}", sanitizer_error))
    _write_sentinel(tc / _FIX_LOOP_EXIT)

    post = next((error for _reason, error in failures if error != "none"), "none")
    passed = not failures and clean_runs == 3
    failure_reason = "; ".join(reason for reason, _error in failures)
    reason = _patch_reason(passed, failure_reason, clean_runs)
    verdict = "PASS" if passed else "FAIL"
    _write_patch_verdict(
        tc,
        verdict,
        reason,
        ctx.pre,
        post,
        ctx.apply_status,
        "success",
        clean_runs,
        ctx.patched_files,
    )
    summary = f"Patch validation {verdict}: {reason}"
    if passed:
        return ProcedureResult(success=True, summary=summary, evidence=tuple(evidence))
    return _failed(_PROCEDURE_PATCH, summary, reason, evidence)


def _patch_reason(passed: bool, failure_reason: str, clean_runs: int) -> str:
    """Post-loop verdict reason (apply-clean + build-success are already guaranteed here)."""
    if passed:
        return "patch applies clean, rebuilds, and the repro is no-crash 3/3 with no error"
    if failure_reason:
        return failure_reason
    return f"repro still crashes ({clean_runs}/3 runs clean)"


def _write_patch_verdict(
    tc: Path,
    verdict: str,
    reason: str,
    pre: str,
    post: str,
    apply_status: str,
    build_status: str,
    no_crash: int,
    patched_files: str,
) -> None:
    values = {
        "VERDICT": verdict,
        "REASON": reason,
        "PRE_PATCH_SANITIZER_ERROR": pre,
        "POST_PATCH_SANITIZER_ERROR": post,
        "PATCH_APPLY_STATUS": apply_status,
        "BUILD_STATUS": build_status,
        "REPRO_RUNS_NO_CRASH": f"{no_crash}/3",
        "PATCHED_FILES": patched_files,
    }
    _write_verdict_file(tc / _PATCH_VERDICT, PATCH_VALIDATION_FIELDS, values)


def _apply_status(outcome: CommandOutcome) -> str:
    if outcome.timed_out:
        return "failed"
    if outcome.exit_code == 0:
        return "clean"
    if "conflict" in outcome.output.lower():
        return "conflicts"
    return "failed"


def _post_patch_run_failure(
    outcome: CommandOutcome,
    signature: CrashSignature,
    *,
    expected_exit_code: int | None,
) -> tuple[str, str] | None:
    """Return the fail-closed post-patch outcome and sanitizer label, if any."""
    sanitizer_error = _sanitizer_error(outcome.output, signature)
    if outcome.timed_out:
        return "timed out", sanitizer_error
    signal = _signal_from_exit(outcome.exit_code)
    if signal == 6 or _ASSERTION_FAILURE_RX.search(outcome.output):
        return "aborted on an assertion", sanitizer_error
    if signal is not None:
        return f"terminated by signal {signal}", sanitizer_error
    accepted_exit_codes = {0}
    if expected_exit_code is not None:
        accepted_exit_codes.add(expected_exit_code)
    if outcome.exit_code not in accepted_exit_codes:
        return f"exited nonzero ({outcome.exit_code})", sanitizer_error
    if sanitizer_error != "none":
        return f"reported sanitizer error {sanitizer_error}", sanitizer_error
    if _UNSANITIZED_CRASH_RX.search(outcome.output):
        return "reported an unsanitized crash", "none"
    return None


def _sanitizer_error(output: str, signature: CrashSignature) -> str:
    if signature.crashed:
        return signature.sanitizer_class or "unknown"
    for marker, label in _SANITIZER_MARKERS:
        if marker in output:
            return label
    return "none"


def _pre_patch_error(tc: Path) -> str:
    match = _OBSERVED_ERROR_RX.search(_read(tc / _EXPLOIT_VERDICT))
    return match.group(1).strip() if match else "unknown"


def _patched_files(diff_text: str) -> str:
    out: list[str] = []
    for path in _DIFF_GIT_RX.findall(diff_text):
        if path not in out:
            out.append(path)
    return ", ".join(out)


# ---------------------------------------------------------------------------
# Command execution + evidence
# ---------------------------------------------------------------------------


async def _run_cmd(
    session: ProcedureSession,
    argv: tuple[str, ...],
    *,
    timeout: float,
    evidence: list[ProcedureEvidence],
    stdin: bytes | None = None,
    env: tuple[tuple[str, str], ...] = (),
) -> CommandOutcome:
    outcome = await session.run(argv, timeout=timeout, stdin=stdin, env=env)
    evidence.append(_evidence_for(argv, outcome))
    return outcome


def _evidence_for(argv: tuple[str, ...], outcome: CommandOutcome) -> ProcedureEvidence:
    digest = hashlib.sha256(outcome.output.encode("utf-8", "replace")).hexdigest()
    return ProcedureEvidence(
        argv=argv,
        exit_code=outcome.exit_code,
        output_sha256=digest,
        excerpt=_tail(outcome.output, _EXCERPT_LIMIT),
    )


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _consensus(sigs: list[CrashSignature]) -> tuple[CrashSignature, int]:
    """The modal crash signature and how many runs share it (0 if none crashed)."""
    crashed = [sig for sig in sigs if sig.crashed]
    if not crashed:
        return _EMPTY_SIG, 0
    reference, _count = Counter(crashed).most_common(1)[0]
    determinism = sum(1 for sig in sigs if sig == reference)
    return reference, determinism


def _terminations_consistent(outcomes: list[CommandOutcome]) -> bool:
    return len({_termination_identity(outcome.exit_code) for outcome in outcomes}) == 1


def _termination_identity(exit_code: int) -> tuple[str, int]:
    signal = _signal_from_exit(exit_code)
    return ("signal", signal) if signal is not None else ("exit", exit_code)


def _signal_from_exit(exit_code: int) -> int | None:
    if exit_code < 0:
        return -exit_code
    if 129 <= exit_code <= 192:
        return exit_code - 128
    return None


def _root_id_from_params(params: dict[str, Any]) -> UUID:
    raw = params.get("root_id")
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError as exc:
            raise ProcedureInfrastructureError(
                f"params['root_id'] is not a UUID: {raw!r}"
            ) from exc
    raise ProcedureInfrastructureError("params must carry 'root_id' (the run's root agent UUID)")


def _procedure_attempt(params: dict[str, Any]) -> int:
    raw = params.get(_PROCEDURE_ATTEMPT_PARAM, 1)
    if type(raw) is not int or raw not in (1, 2):
        raise ProcedureInfrastructureError(
            f"params[{_PROCEDURE_ATTEMPT_PARAM!r}] must be 1 or 2"
        )
    return raw


def _procedure_resume(params: dict[str, Any]) -> bool:
    raw = params.get(_PROCEDURE_RESUME_PARAM, False)
    if type(raw) is not bool:
        raise ProcedureInfrastructureError(
            f"params[{_PROCEDURE_RESUME_PARAM!r}] must be a boolean"
        )
    return raw


def _failed(
    procedure_ref: str, summary: str, reason: str, evidence: list[ProcedureEvidence]
) -> ProcedureResult:
    return ProcedureResult(
        success=False,
        summary=summary,
        digest=_build_digest(procedure_ref, reason, evidence),
        evidence=tuple(evidence),
    )


def _build_digest(procedure_ref: str, reason: str, evidence: list[ProcedureEvidence]) -> str:
    lines = [f"PROCEDURE {procedure_ref} FAILED: {reason}", "COMMANDS:"]
    for item in evidence:
        lines.append(f"$ {' '.join(item.argv)} -> exit={item.exit_code}")
        if item.excerpt:
            lines.append(item.excerpt)
    return _head_tail("\n".join(lines), _DIGEST_LIMIT)


def _timeout_or(outcome: CommandOutcome, otherwise: str) -> str:
    return f"{otherwise} (timed out)" if outcome.timed_out else otherwise


def _write_run_log(tc: Path, name: str, outcome: CommandOutcome) -> None:
    body = outcome.output
    if body and not body.endswith("\n"):
        body += "\n"
    _write_file(tc / name, f"{body}exit={outcome.exit_code}\n")


def _write_verdict_file(
    path: Path, fields: tuple[tuple[str, str], ...], values: dict[str, str]
) -> None:
    lines = [f"{key}: {values[key]}" for key, _description in fields]
    _write_file(path, "\n".join(lines) + "\n")


def _write_sentinel(path: Path) -> None:
    _write_file(path, "done\n")


def _write_file(path: Path, content: str) -> None:
    _atomic_write(path, content, mode=0o666)


def _atomic_write(path: Path, content: str, *, mode: int) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"), mode=mode)


def _atomic_write_bytes(path: Path, content: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _clear_logs(tc: Path, pattern: str) -> None:
    for stale in tc.glob(pattern):
        stale.unlink()


def _read(path: Path) -> str:
    try:
        return _read_regular_bytes(path).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(f"refusing to read non-regular file: {path}")
        return stream.read()


def _regular_file_size(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return -1
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        return status.st_size if stat.S_ISREG(status.st_mode) else -1


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _join(parts: list[str]) -> str:
    return "\n".join(parts) + "\n"


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[-limit:]


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    note = f"\n...[{len(text) - limit} chars omitted]...\n"
    budget = max(limit - len(note), 0)
    head = budget // 2
    tail = budget - head
    return text[:head] + note + (text[-tail:] if tail else "")


if TYPE_CHECKING:

    def _port_conformance(executor: SecBenchProcedureExecutor) -> ProcedureExecutorPort:
        """Static assertion that the executor satisfies the port (no runtime effect)."""
        return executor
