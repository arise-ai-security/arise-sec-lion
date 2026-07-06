"""SEC-bench deterministic procedure executor (Exploit-Validator / Patch-Validator).

Runs the two fixed validator roles host-side with zero LLM turns, driving the
run's existing container session synchronously from Python (no detached-nohup /
poll dance). The verdict files are host-computed and event-sourced, so validator
verdicts become agent-unforgeable by construction (SYSTEM_REFERENCE §V.7).

On-disk artifacts (``exploit_validation_results.txt``, ``patch_validation_results.txt``,
``repro_run_*.log`` / ``fix_run_*.log`` and their ``.exit`` sentinels) keep the exact
names and format the prompt contract mandates, so downstream consumers (Reporter
prompts, ``criteria.py``, dashboards) are unchanged.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from core.domain.values.procedure import ProcedureEvidence, ProcedureResult
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
from plugins.security.prompt_strategy import _role_from_task


if TYPE_CHECKING:
    from collections.abc import Callable

    from core.ports.procedure_ports import ProcedureExecutorPort


# Per-command timeouts (seconds). settings.security carries no build/repro-specific
# knob today (only a generic worker docker timeout), so these are module constants.
_BUILD_TIMEOUT = 1800.0
_REPRO_TIMEOUT = 600.0
_PATCH_TIMEOUT = 600.0
_PREFLIGHT_TIMEOUT = 60.0

_DIGEST_LIMIT = 4000
_EXCERPT_LIMIT = 500

# The seeded /testcase/repro.sh stub the runtime writes at prep and expects the
# Exploiter to replace (plugins/security/docker_runtime.py:_REPRO_SKELETON).
_REPRO_SKELETON_MARKER = "Arise seeded an empty"

# Static Tier-1 registry: role bracket -> procedure_ref.
_PROCEDURE_EXPLOIT = "secb_exploit_validation"
_PROCEDURE_PATCH = "secb_patch_validation"
_REGISTRY: dict[str, str] = {
    "Exploit-Validator": _PROCEDURE_EXPLOIT,
    "Patch-Validator": _PROCEDURE_PATCH,
}
_PROCEDURE_REFS = frozenset(_REGISTRY.values())

# Canonical /testcase filenames, derived from the deliverables contract.
_REPRO_SH = PurePosixPath(ARTIFACT_PATHS["repro_script"]).name
_POC_POINTER = PurePosixPath(ARTIFACT_PATHS["poc_path"]).name
_BINARY_POINTER = PurePosixPath(ARTIFACT_PATHS["binary_paths"]).name
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


class ProcedureSession(Protocol):
    """The run's container-session slice a procedure drives.

    ``testcase_dir`` is the host mirror of the container ``/testcase`` bind mount
    (where deliverables and logs land). ``run`` executes a shell command inside the
    run's container and returns its exit code + combined output; a timeout returns
    ``CommandOutcome(timed_out=True, ...)`` with the partial output rather than
    raising (a timeout is a task-level failure, not an infrastructure fault).
    """

    testcase_dir: Path

    async def run(self, command: str, *, timeout: float) -> CommandOutcome: ...


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
        role = _role_from_task(task_description)
        if role is None:
            return None
        return _REGISTRY.get(role.name)

    def resolve(self, procedure_ref: str) -> bool:
        """True iff the ref names a registered procedure (validates LLM markings)."""
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
        if procedure_ref == _PROCEDURE_EXPLOIT:
            return await _run_exploit_validation(session, cve)
        if procedure_ref == _PROCEDURE_PATCH:
            return await _run_patch_validation(session, cve)
        raise ProcedureInfrastructureError(f"unknown procedure_ref {procedure_ref!r}")


# ---------------------------------------------------------------------------
# Exploit validation
# ---------------------------------------------------------------------------


async def _run_exploit_validation(
    session: ProcedureSession, cve: CVEInstance | None
) -> ProcedureResult:
    evidence: list[ProcedureEvidence] = []
    tc = session.testcase_dir
    golden = compute_crash_signature(cve.sanitizer_report) if cve is not None else _EMPTY_SIG

    preflight = await _exploit_preflight(session, evidence)
    if preflight is not None:
        _write_exploit_verdict(tc, "FAIL", preflight, golden, _EMPTY_SIG, 0)
        summary = f"Exploit validation FAIL: {preflight}"
        return _failed(_PROCEDURE_EXPLOIT, summary, preflight, evidence)

    _clear_logs(tc, "repro_run_*.log")
    run_sigs: list[CrashSignature] = []
    for index in (1, 2, 3):
        outcome = await _run_cmd(session, "secb repro", timeout=_REPRO_TIMEOUT, evidence=evidence)
        _write_run_log(tc, _REPRO_RUN_LOG.replace("$N", str(index)), outcome)
        if outcome.timed_out:
            reason = f"secb repro timed out on run {index}"
            _write_exploit_verdict(tc, "FAIL", reason, golden, _EMPTY_SIG, index - 1)
            summary = f"Exploit validation FAIL: {reason}"
            return _failed(_PROCEDURE_EXPLOIT, summary, reason, evidence)
        run_sigs.append(compute_crash_signature(outcome.output))
    _write_sentinel(tc / _REPRO_LOOP_EXIT)

    observed, determinism = _consensus(run_sigs)
    matched = signatures_match(observed, golden)
    passed = matched and determinism == 3
    reason = _exploit_reason(passed, matched, determinism, observed, golden)
    verdict = "PASS" if passed else "FAIL"
    _write_exploit_verdict(tc, verdict, reason, golden, observed, determinism)
    summary = f"Exploit validation {verdict}: {reason}"
    if passed:
        return ProcedureResult(success=True, summary=summary, evidence=tuple(evidence))
    return _failed(_PROCEDURE_EXPLOIT, summary, reason, evidence)


async def _exploit_preflight(
    session: ProcedureSession, evidence: list[ProcedureEvidence]
) -> str | None:
    """Return a failure reason string, or None when preflight passes."""
    tc = session.testcase_dir
    repro = _read(tc / _REPRO_SH)
    if not repro.strip():
        return f"{ARTIFACT_PATHS['repro_script']} missing or empty"
    if _REPRO_SKELETON_MARKER in repro:
        return f"{ARTIFACT_PATHS['repro_script']} is still the seeded skeleton (not replaced)"

    poc_reason = await _pointer_resolves(session, _POC_POINTER, "PoC", evidence)
    if poc_reason is not None:
        return poc_reason
    return await _pointer_resolves(session, _BINARY_POINTER, "Builder binary", evidence)


async def _pointer_resolves(
    session: ProcedureSession, filename: str, kind: str, evidence: list[ProcedureEvidence]
) -> str | None:
    """Reason string if the pointer file is empty or its target is missing, else None."""
    target = _first_line(_read(session.testcase_dir / filename))
    if not target:
        return f"{kind} pointer {filename} names no path"
    if not await _resolves(session, target, evidence):
        return f"{kind} {target} does not resolve to a non-empty file"
    return None


def _exploit_reason(
    passed: bool,
    matched: bool,
    determinism: int,
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
    session: ProcedureSession, cve: CVEInstance | None
) -> ProcedureResult:
    _ = cve
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

    patch_out = await _run_cmd(session, "secb patch", timeout=_PATCH_TIMEOUT, evidence=evidence)
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

    build_out = await _run_cmd(session, "secb build", timeout=_BUILD_TIMEOUT, evidence=evidence)
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

    return await _patch_repro_loop(
        session,
        evidence,
        _PatchContext(tc=tc, pre=pre, apply_status=apply_status, patched_files=patched_files),
    )


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
    ctx: _PatchContext,
) -> ProcedureResult:
    tc = ctx.tc
    _clear_logs(tc, "fix_run_*.log")
    run_sigs: list[CrashSignature] = []
    for index in (1, 2, 3):
        outcome = await _run_cmd(session, "secb repro", timeout=_REPRO_TIMEOUT, evidence=evidence)
        _write_run_log(tc, _FIX_RUN_LOG.replace("$N", str(index)), outcome)
        if outcome.timed_out:
            _write_sentinel(tc / _FIX_LOOP_EXIT)
            reason = f"secb repro timed out on post-patch run {index}"
            _write_patch_verdict(
                tc, "FAIL", reason, ctx.pre, "unknown", ctx.apply_status, "success",
                _no_crash_count(run_sigs), ctx.patched_files,
            )
            return _failed(_PROCEDURE_PATCH, f"Patch validation FAIL: {reason}", reason, evidence)
        run_sigs.append(compute_crash_signature(outcome.output))
    _write_sentinel(tc / _FIX_LOOP_EXIT)

    no_crash = _no_crash_count(run_sigs)
    post = _post_patch_error(run_sigs)
    passed = post == "none" and no_crash == 3
    reason = _patch_reason(passed, post, no_crash)
    verdict = "PASS" if passed else "FAIL"
    _write_patch_verdict(
        tc, verdict, reason, ctx.pre, post, ctx.apply_status, "success", no_crash, ctx.patched_files
    )
    summary = f"Patch validation {verdict}: {reason}"
    if passed:
        return ProcedureResult(success=True, summary=summary, evidence=tuple(evidence))
    return _failed(_PROCEDURE_PATCH, summary, reason, evidence)


def _patch_reason(passed: bool, post: str, no_crash: int) -> str:
    """Post-loop verdict reason (apply-clean + build-success are already guaranteed here)."""
    if passed:
        return "patch applies clean, rebuilds, and the repro is no-crash 3/3 with no error"
    if post != "none":
        return f"sanitizer error {post} still fires after the patch"
    return f"repro still crashes ({no_crash}/3 runs clean)"


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


def _no_crash_count(sigs: list[CrashSignature]) -> int:
    return sum(1 for sig in sigs if not sig.crashed)


def _post_patch_error(sigs: list[CrashSignature]) -> str:
    for sig in sigs:
        if sig.crashed:
            return sig.sanitizer_class or "unknown"
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
    command: str,
    *,
    timeout: float,
    evidence: list[ProcedureEvidence],
) -> CommandOutcome:
    outcome = await session.run(command, timeout=timeout)
    evidence.append(_evidence_for(command, outcome))
    return outcome


async def _resolves(
    session: ProcedureSession, path: str, evidence: list[ProcedureEvidence]
) -> bool:
    command = f"test -s {shlex.quote(path)}"
    outcome = await _run_cmd(session, command, timeout=_PREFLIGHT_TIMEOUT, evidence=evidence)
    return outcome.exit_code == 0 and not outcome.timed_out


def _evidence_for(command: str, outcome: CommandOutcome) -> ProcedureEvidence:
    digest = hashlib.sha256(outcome.output.encode("utf-8", "replace")).hexdigest()
    return ProcedureEvidence(
        argv=_argv(command),
        exit_code=outcome.exit_code,
        output_sha256=digest,
        excerpt=_tail(outcome.output, _EXCERPT_LIMIT),
    )


def _argv(command: str) -> tuple[str, ...]:
    try:
        parts = tuple(shlex.split(command))
    except ValueError:
        return (command,)
    return parts or (command,)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _clear_logs(tc: Path, pattern: str) -> None:
    for stale in tc.glob(pattern):
        stale.unlink()


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


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
