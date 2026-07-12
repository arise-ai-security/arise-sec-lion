"""Tests for the SEC-bench deterministic procedure executor.

Every procedure is exercised against a FAKE container session (no docker, no
network): the fake records the commands it is asked to run and returns scripted
outcomes, while deliverable/log files land in a ``tmp_path`` mirror of ``/testcase``.

The verdict-file writers are pinned to the ``deliverables.py`` field constants
(a drift test), and the plugin crash-signature implementation is pinned to the
``experiments/shared/evaluation/criteria.py`` copy (imported by the test only —
production code must never cross that boundary).
"""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from plugins.security.crash_signature import compute_crash_signature, signatures_match
from plugins.security.cve_instance import CVEInstance
from plugins.security.deliverables import EXPLOIT_VALIDATION_FIELDS, PATCH_VALIDATION_FIELDS
from plugins.security.procedures import (
    CommandOutcome,
    ProcedureInfrastructureError,
    SecBenchProcedureExecutor,
)


# ---------------------------------------------------------------------------
# Fixtures: sanitizer reports, repro/patch inputs, and the fake session.
# ---------------------------------------------------------------------------

_ASAN_HEAP = """=================================================================
==4127==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x602000000d10
READ of size 4 at 0x602000000d10 thread T0
    #0 0x4f2a1b in ngx_parse_header /src/nginx/src/http/ngx_http_parse.c:210:12
    #1 0x4f0a00 in ngx_http_process_request /src/nginx/src/http/ngx_http_request.c:1520:9
    #2 0x7f1234 in __libc_start_main
"""

_ASAN_STACK = """==99==ERROR: AddressSanitizer: stack-buffer-overflow on address 0x1
WRITE of size 8 at 0x1 thread T0
    #0 0x2 in other_func /src/nginx/src/core/ngx_string.c:50:3
    #1 0x3 in main /src/nginx/src/core/nginx.c:20
"""

_UBSAN = (
    "/src/p/foo.c:12:9: runtime error: signed integer overflow: 2147483647 + 1 "
    "cannot be represented in type 'int'\n"
)

_LSAN = """==12==ERROR: LeakSanitizer: detected memory leaks
Direct leak of 16 byte(s) in 1 object(s) allocated from:
    #0 0x4a in malloc
    #1 0x4b in make_thing /src/p/thing.c:88:10
"""

_NO_CRASH = "running repro...\nall checks passed, no sanitizer error observed\n"

_SIG_FIXTURES = (_ASAN_HEAP, _ASAN_STACK, _UBSAN, _LSAN, _NO_CRASH)

_REAL_REPRO = (
    "#!/bin/bash\ncd /work\n"
    '"$(head -1 /testcase/binary_paths.txt)" < /testcase/poc_path.txt\n'
)
_SKELETON_REPRO = (
    "#!/bin/bash\nset -euo pipefail\n"
    'echo "Arise seeded an empty /testcase/repro.sh; Exploiter must replace it." >&2\nexit 2\n'
)

_MODEL_PATCH_DIFF = """diff --git a/src/foo.c b/src/foo.c
index 1111111..2222222 100644
--- a/src/foo.c
+++ b/src/foo.c
@@ -1,3 +1,3 @@
 keep
-bad
+good
"""


@dataclass
class _Resp:
    """One scripted response, matched (FIFO) when ``needle`` is in the command."""

    needle: str
    outcome: CommandOutcome


class FakeSession:
    """A ProcedureSession that records commands and returns scripted outcomes."""

    def __init__(
        self,
        testcase_dir: Path,
        responses: list[_Resp] | None = None,
        default: CommandOutcome | None = None,
    ) -> None:
        self.testcase_dir = testcase_dir
        self._responses = list(responses or [])
        self._default = default or CommandOutcome(exit_code=0, output="")
        self.commands: list[str] = []

    async def run(self, command: str, *, timeout: float) -> CommandOutcome:
        _ = timeout
        self.commands.append(command)
        for i, resp in enumerate(self._responses):
            if resp.needle in command:
                del self._responses[i]
                return resp.outcome
        return self._default


def _cve() -> CVEInstance:
    return CVEInstance(
        instance_id="nginx.cve-2024-0001",
        repo="nginx/nginx",
        project_name="nginx",
        lang="c",
        work_dir="/work",
        sanitizer="address",
        bug_description="Heap overflow parsing headers.",
        base_commit="a" * 40,
        sanitizer_report=_ASAN_HEAP,
        bug_report=_ASAN_HEAP,
    )


def _executor(session: FakeSession | None) -> SecBenchProcedureExecutor:
    return SecBenchProcedureExecutor(lambda _root_id: session)


def _crash(output: str) -> CommandOutcome:
    return CommandOutcome(exit_code=1, output=output)


def _clean(output: str) -> CommandOutcome:
    return CommandOutcome(exit_code=0, output=output)


def _repro(outcome: CommandOutcome, times: int = 3) -> list[_Resp]:
    return [_Resp("secb repro", outcome) for _ in range(times)]


def _seed_exploit_inputs(tc: Path, *, repro: str = _REAL_REPRO) -> None:
    tc.mkdir(parents=True, exist_ok=True)
    (tc / "repro.sh").write_text(repro)
    (tc / "poc_path.txt").write_text("/testcase/poc.bin\n")
    (tc / "binary_paths.txt").write_text("/work/bin/app\n")


def _seed_patch_inputs(tc: Path, *, observed: str = "heap-buffer-overflow") -> None:
    tc.mkdir(parents=True, exist_ok=True)
    (tc / "model_patch.diff").write_text(_MODEL_PATCH_DIFF)
    (tc / "exploit_validation_results.txt").write_text(
        f"VERDICT: PASS\nOBSERVED_SANITIZER_ERROR: {observed}\n"
    )


def _verdict_keys(path: Path) -> list[str]:
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return [line.split(":", 1)[0] for line in lines]


# ---------------------------------------------------------------------------
# match / resolve
# ---------------------------------------------------------------------------


def test_match_maps_validator_brackets_to_procedures() -> None:
    # Given: an executor and the two catalog validator brackets
    ex = _executor(None)
    cve = _cve()

    # When / Then: each validator bracket maps to its procedure_ref
    assert ex.match("[Build-Verifier] verify the binary", cve) == "secb_build_validation"
    assert ex.match("[Exploit-Validator] validate the repro", cve) == "secb_exploit_validation"
    assert ex.match("[Patch-Validator] validate the patch", cve) == "secb_patch_validation"


async def test_build_validation_passes_when_build_succeeds_and_binary_resolves(
    tmp_path: Path,
) -> None:
    # Given: a declared binary pointer and a session where secb build succeeds
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    session = FakeSession(tc)  # default exit 0 → build ok, binary resolves
    ex = _executor(session)

    # When: the build-validation procedure runs
    result = await ex.execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    # Then: it passes host-side and records the secb build command as evidence
    assert result.success is True
    assert any("secb build" in command for command in session.commands)


async def test_build_validation_fails_when_build_fails(tmp_path: Path) -> None:
    # Given: a session where secb build returns a nonzero exit
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    session = FakeSession(
        tc, responses=[_Resp(needle="secb build", outcome=CommandOutcome(exit_code=1, output="err"))]
    )
    ex = _executor(session)

    # When: the build-validation procedure runs
    result = await ex.execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    # Then: the host verdict is FAIL — the agent cannot override it
    assert result.success is False


async def test_build_validation_fails_when_binary_pointer_missing(tmp_path: Path) -> None:
    # Given: secb build succeeds but no binary pointer was declared
    tc = tmp_path / "testcase"
    tc.mkdir()
    session = FakeSession(tc)  # build ok, but binary_paths.txt absent
    ex = _executor(session)

    # When: the build-validation procedure runs
    result = await ex.execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    # Then: it fails — a build without a resolvable binary is not a valid Builder outcome
    assert result.success is False


def test_match_returns_none_for_unregistered_or_missing_bracket() -> None:
    # Given: an executor
    ex = _executor(None)
    cve = _cve()

    # When / Then: non-validator roles, unknown labels, and bare tasks do not match
    assert ex.match("[Repro-Creator] build the repro script", cve) is None
    assert ex.match("[Totally-Made-Up] do a thing", cve) is None
    assert ex.match("reproduce the vulnerability with no bracket", cve) is None


def test_resolve_accepts_only_registered_refs() -> None:
    # Given: an executor
    ex = _executor(None)

    # When / Then: only the registered refs resolve
    assert ex.resolve("secb_build_validation") is True
    assert ex.resolve("secb_exploit_validation") is True
    assert ex.resolve("secb_patch_validation") is True
    assert ex.resolve("secb_unregistered_procedure") is False
    assert ex.resolve("") is False


# ---------------------------------------------------------------------------
# Exploit validation
# ---------------------------------------------------------------------------


async def test_exploit_validation_pass_writes_verdict_and_logs(tmp_path: Path) -> None:
    # Given: valid inputs and a repro that deterministically reproduces the oracle crash
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))
    ex = _executor(session)

    # When: the exploit-validation procedure runs
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator] x", _cve(), {"root_id": uuid4()}
    )

    # Then: it passes and the verdict file carries the PASS + determinism tokens
    assert result.success is True
    verdict = (tc / "exploit_validation_results.txt").read_text()
    assert "VERDICT: PASS" in verdict
    assert "DETERMINISM_RUNS: 3/3" in verdict
    assert "OBSERVED_SANITIZER_ERROR: heap-buffer-overflow" in verdict
    assert "CRASH_FUNCTION_OBSERVED: ngx_parse_header" in verdict

    # And: the canonical per-run logs and the loop sentinel are written
    for n in (1, 2, 3):
        log = (tc / f"repro_run_{n}.log").read_text()
        assert "AddressSanitizer" in log
        assert "exit=1" in log
    assert (tc / "repro_loop.exit").read_text().strip() == "done"


async def test_exploit_validation_signature_mismatch_fails(tmp_path: Path) -> None:
    # Given: a repro that deterministically crashes at the WRONG site
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_STACK)))
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: deterministic (3/3) but the signature does not match -> FAIL
    assert result.success is False
    verdict = (tc / "exploit_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "DETERMINISM_RUNS: 3/3" in verdict
    assert "does not match" in verdict


async def test_exploit_validation_nondeterministic_two_of_three_fails(tmp_path: Path) -> None:
    # Given: two runs crash with the oracle signature, the third does not crash
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb repro", _crash(_ASAN_HEAP)),
            _Resp("secb repro", _crash(_ASAN_HEAP)),
            _Resp("secb repro", _clean(_NO_CRASH)),
        ],
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: not deterministic (2/3) -> FAIL even though the signature matched
    assert result.success is False
    verdict = (tc / "exploit_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "DETERMINISM_RUNS: 2/3" in verdict


async def test_exploit_validation_skeleton_repro_preflight_fails(tmp_path: Path) -> None:
    # Given: repro.sh is still the seeded skeleton
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc, repro=_SKELETON_REPRO)
    session = FakeSession(tc)
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: preflight fails, no secb repro is ever launched, verdict still written
    assert result.success is False
    assert not any("secb repro" in c for c in session.commands)
    verdict = (tc / "exploit_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "skeleton" in verdict


async def test_exploit_validation_unresolved_binary_preflight_fails(tmp_path: Path) -> None:
    # Given: the declared Builder binary does not resolve to a real file
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(
        tc, responses=[_Resp("/work/bin/app", CommandOutcome(exit_code=1, output=""))]
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: preflight fails on the binary, repro never runs
    assert result.success is False
    assert not any("secb repro" in c for c in session.commands)
    assert "does not resolve" in (tc / "exploit_validation_results.txt").read_text()


async def test_exploit_validation_repro_timeout_returns_failed_with_digest(
    tmp_path: Path,
) -> None:
    # Given: the first secb repro times out with partial output
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    timeout_outcome = CommandOutcome(
        exit_code=-1, output="partial repro output before the timeout", timed_out=True
    )
    session = FakeSession(tc, responses=[_Resp("secb repro", timeout_outcome)])
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: a failed result whose digest carries the timeout reason and partial tail
    assert result.success is False
    assert result.digest
    assert "timed out" in result.summary
    assert "partial repro output before the timeout" in result.digest


async def test_exploit_verdict_keys_match_deliverables_contract(tmp_path: Path) -> None:
    # Given: any exploit run (the keys must not drift from the contract constant)
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))
    ex = _executor(session)

    # When
    await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: the verdict-file block keys equal EXPLOIT_VALIDATION_FIELDS in order
    keys = _verdict_keys(tc / "exploit_validation_results.txt")
    assert keys == [key for key, _desc in EXPLOIT_VALIDATION_FIELDS]


async def test_exploit_records_evidence_per_command(tmp_path: Path) -> None:
    # Given: a passing exploit run (2 preflight resolves + 3 repro launches)
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: one ProcedureEvidence per executed command, each fully populated
    assert len(result.evidence) == 5
    for item in result.evidence:
        assert item.argv
        assert len(item.output_sha256) == 64
        assert len(item.excerpt) <= 500


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------


async def test_patch_validation_pass_writes_verdict_and_logs(tmp_path: Path) -> None:
    # Given: a clean apply, a successful build, and 3/3 no-crash repro
    tc = tmp_path / "testcase"
    _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            *_repro(_clean(_NO_CRASH)),
        ],
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: PASS with the full contract tokens and the pre-patch error carried across
    assert result.success is True
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: PASS" in verdict
    assert "PATCH_APPLY_STATUS: clean" in verdict
    assert "BUILD_STATUS: success" in verdict
    assert "REPRO_RUNS_NO_CRASH: 3/3" in verdict
    assert "PATCHED_FILES: src/foo.c" in verdict
    assert "PRE_PATCH_SANITIZER_ERROR: heap-buffer-overflow" in verdict

    # And: the fix-loop artifacts match the on-disk contract
    fix_loop = (tc / "fix_loop.log").read_text()
    assert "patch_exit=0" in fix_loop
    assert "build_exit=0" in fix_loop
    for n in (1, 2, 3):
        assert (tc / f"fix_run_{n}.log").read_text().rstrip().endswith("exit=0")
    assert (tc / "fix_loop.exit").read_text().strip() == "done"


async def test_patch_validation_dirty_apply_fails_and_skips_build(tmp_path: Path) -> None:
    # Given: secb patch reports a conflict
    tc = tmp_path / "testcase"
    _seed_patch_inputs(tc)
    session = FakeSession(
        tc, responses=[_Resp("secb patch", _crash("error: patch failed: conflict in foo.c"))]
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: FAIL, and neither build nor repro is attempted
    assert result.success is False
    assert not any("secb build" in c for c in session.commands)
    assert not any("secb repro" in c for c in session.commands)
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "PATCH_APPLY_STATUS: conflicts" in verdict


async def test_patch_validation_build_failure_fails_and_skips_repro(tmp_path: Path) -> None:
    # Given: apply clean but the rebuild fails
    tc = tmp_path / "testcase"
    _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _crash("compile error: undefined reference")),
        ],
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: FAIL with BUILD_STATUS failed and no repro attempted
    assert result.success is False
    assert not any("secb repro" in c for c in session.commands)
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "BUILD_STATUS: failed" in verdict


async def test_patch_validation_empty_diff_fails_without_running_secb(tmp_path: Path) -> None:
    # Given: an empty model_patch.diff
    tc = tmp_path / "testcase"
    tc.mkdir(parents=True)
    (tc / "model_patch.diff").write_text("")
    session = FakeSession(tc)
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: FAIL immediately, no secb command run, digest present
    assert result.success is False
    assert session.commands == []
    assert result.digest
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "missing or empty" in verdict


async def test_patch_validation_post_patch_crash_fails(tmp_path: Path) -> None:
    # Given: apply + build succeed but the repro still crashes once
    tc = tmp_path / "testcase"
    _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            _Resp("secb repro", _crash(_ASAN_HEAP)),
            _Resp("secb repro", _clean(_NO_CRASH)),
            _Resp("secb repro", _clean(_NO_CRASH)),
        ],
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: FAIL with the surviving sanitizer error recorded and 2/3 clean
    assert result.success is False
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "POST_PATCH_SANITIZER_ERROR: heap-buffer-overflow" in verdict
    assert "REPRO_RUNS_NO_CRASH: 2/3" in verdict


async def test_patch_validation_build_timeout_returns_failed_with_digest(tmp_path: Path) -> None:
    # Given: apply clean but the build times out with partial output
    tc = tmp_path / "testcase"
    _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp(
                "secb build",
                CommandOutcome(exit_code=-1, output="partial build log tail", timed_out=True),
            ),
        ],
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: failed result with a digest carrying the partial build output
    assert result.success is False
    assert result.digest
    assert "timed out" in result.summary
    assert "partial build log tail" in result.digest


async def test_patch_verdict_keys_match_deliverables_contract(tmp_path: Path) -> None:
    # Given: any patch run
    tc = tmp_path / "testcase"
    _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            *_repro(_clean(_NO_CRASH)),
        ],
    )
    ex = _executor(session)

    # When
    await ex.execute("secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()})

    # Then: the verdict-file block keys equal PATCH_VALIDATION_FIELDS in order
    keys = _verdict_keys(tc / "patch_validation_results.txt")
    assert keys == [key for key, _desc in PATCH_VALIDATION_FIELDS]


# ---------------------------------------------------------------------------
# Infrastructure faults (execute may raise; caller converts to a failed result)
# ---------------------------------------------------------------------------


async def test_execute_raises_when_no_session_resolves() -> None:
    # Given: a resolver that finds no container session for the run
    ex = SecBenchProcedureExecutor(lambda _root_id: None)

    # When / Then: an infrastructure fault is raised (not a FAIL result)
    with pytest.raises(ProcedureInfrastructureError):
        await ex.execute(
            "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
        )


async def test_execute_raises_when_root_id_missing(tmp_path: Path) -> None:
    # Given: params without the required root_id
    session = FakeSession(tmp_path / "testcase")
    ex = _executor(session)

    # When / Then: the wiring bug surfaces as an infrastructure fault
    with pytest.raises(ProcedureInfrastructureError):
        await ex.execute("secb_exploit_validation", "[Exploit-Validator]", _cve(), {})


# ---------------------------------------------------------------------------
# Crash-signature drift test vs experiments/shared/evaluation/criteria.py
# ---------------------------------------------------------------------------


def test_crash_signature_extraction_matches_criteria() -> None:
    # Given: a set of realistic ASan / UBSan / LeakSan / no-crash snippets. The eval
    # package is imported lazily inside the test (a test-only boundary cross; it is
    # not on sys.path at module-collection time and production code must never import it).
    from experiments.shared.evaluation import criteria

    # When / Then: the plugin extraction agrees with the criteria.py copy verbatim
    for text in _SIG_FIXTURES:
        assert compute_crash_signature(text).as_tuple() == criteria._crash_signature(text)


def _run_data(run_dir: Path, observed: str):
    from experiments.shared.evaluation.models import RunData

    tc = run_dir / "testcase"
    tc.mkdir(parents=True, exist_ok=True)
    (tc / "repro_run_1.log").write_text(observed)
    return RunData(run_id=uuid4(), events=[], run_dir=run_dir, manifest={})


def test_signature_match_decisions_agree_with_criteria(tmp_path: Path) -> None:
    # Given: the golden oracle crash and one matching + one non-matching observed run.
    # Eval package imported lazily (test-only boundary cross; see extraction test).
    from experiments.shared.evaluation import criteria
    from experiments.shared.evaluation.models import CveOracle

    oracle = CveOracle(
        instance_id="nginx.cve-2024-0001",
        sanitizer="address",
        sanitizer_report=_ASAN_HEAP,
        bug_report=_ASAN_HEAP,
        bug_description="",
    )
    golden = compute_crash_signature(_ASAN_HEAP)

    # When / Then: the match case agrees (both True)
    match_run = _run_data(tmp_path / "match", _ASAN_HEAP)
    mine_match = signatures_match(compute_crash_signature(_ASAN_HEAP), golden)
    theirs_match = criteria._crash_signature_matches(match_run, oracle)
    assert mine_match is True
    assert mine_match == theirs_match

    # And: the no-match case agrees (both False)
    miss_run = _run_data(tmp_path / "miss", _ASAN_STACK)
    mine_miss = signatures_match(compute_crash_signature(_ASAN_STACK), golden)
    theirs_miss = criteria._crash_signature_matches(miss_run, oracle)
    assert mine_miss is False
    assert mine_miss == theirs_miss
