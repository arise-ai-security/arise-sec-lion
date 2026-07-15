"""Tests for the SEC-bench deterministic procedure executor.

Every procedure is exercised against a FAKE container session (no docker, no
network): the fake records the commands it is asked to run and returns scripted
outcomes, while deliverable/log files land in a ``tmp_path`` mirror of ``/testcase``.

The verdict-file writers are pinned to the ``deliverables.py`` field constants.
"""

import hashlib
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from plugins.security.cve_instance import CVEInstance
from plugins.security.deliverables import EXPLOIT_VALIDATION_FIELDS, PATCH_VALIDATION_FIELDS
from plugins.security.procedures import (
    CommandOutcome,
    CommandTermination,
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

_NO_CRASH = "running repro...\nall checks passed, no sanitizer error observed\n"
_ASAN_HELP = "Available flags for AddressSanitizer:"
_FAAD_BINARY = "/src/faad2/frontend/.libs/faad"
_FAAD_LIBRARY_DIR = "/src/faad2/libfaad/.libs"
_FAAD_LOADER_PATH = f"{_FAAD_LIBRARY_DIR}:/opt/runtime/lib"
_FAAD_MISSING_LIBRARY = (
    f"{_FAAD_BINARY}: error while loading shared libraries: libfaad.so.2: "
    "cannot open shared object file: No such file or directory"
)

_REAL_REPRO = (
    "#!/bin/bash\n"
    "set -euo pipefail\n"
    'BIN="$(head -n 1 /testcase/binary_paths.txt)"\n'
    'POC="$(head -n 1 /testcase/poc_path.txt)"\n'
    'exec "$BIN" < "$POC"\n'
)


def _repro_script(final_command: str) -> str:
    return "\n".join((*_REAL_REPRO.splitlines()[:-1], final_command, ""))


_SKELETON_REPRO = (
    "#!/bin/bash\nset -euo pipefail\n"
    'echo "Arise seeded an empty /testcase/repro.sh; Exploiter must replace it." >&2\nexit 2\n'
)


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
        source_dir: Path | None = None,
        work_dir: Path | None = None,
        identity_dir: Path | None = None,
    ) -> None:
        self.testcase_dir = testcase_dir
        self.source_dir = source_dir or testcase_dir.parent / "src"
        self.work_dir = work_dir or testcase_dir.parent / "work"
        self.identity_dir = identity_dir or testcase_dir.parent / f"{testcase_dir.name}.sealed"
        self._responses = list(responses or [])
        self._default = default or CommandOutcome(exit_code=0, output="")
        self.commands: list[str] = []
        self.invocations: list[
            tuple[tuple[str, ...], bytes | None, tuple[tuple[str, str], ...], float]
        ] = []
        self.marker_invocations: list[
            tuple[tuple[str, ...], str, tuple[tuple[str, str], ...], float]
        ] = []

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome:
        command = " ".join(
            (*[f"{name}={shlex.quote(value)}" for name, value in env], shlex.join(argv))
        )
        self.commands.append(command)
        self.invocations.append((argv, stdin, env, timeout))
        for i, resp in enumerate(self._responses):
            if resp.needle in command:
                del self._responses[i]
                return resp.outcome
        return self._default

    async def run_until_marker(
        self,
        argv: tuple[str, ...],
        *,
        marker: str,
        timeout: float,
        env: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome:
        self.marker_invocations.append((argv, marker, env, timeout))
        outcome = await self.run(argv, timeout=timeout, env=env)
        if (
            marker in outcome.output
            and outcome.termination is CommandTermination.COMPLETED
        ):
            return CommandOutcome(
                exit_code=outcome.exit_code,
                output=outcome.output,
                termination=CommandTermination.STOPPED_AFTER_MARKER,
            )
        return outcome


class StatefulPatchSession(FakeSession):
    """Patch session that mutates the shared source and binary like the real runtime."""

    def __init__(
        self,
        testcase_dir: Path,
        *,
        original_source: bytes = b"void parse(void) { unsafe(); }\n",
        patched_source: bytes = b"void parse(void) { safe(); }\n",
        binary_path: str = "/work/bin/app",
    ) -> None:
        super().__init__(
            testcase_dir,
            responses=[
                _Resp("secb patch", _clean("applied")),
                _Resp("secb patch", _clean("applied")),
                _Resp("secb build", _crash("compile failed after replacing the binary")),
                _Resp("secb build", _clean("rebuilt")),
                *_binary_validation(binary_path),
                *_direct_replay(_clean(_NO_CRASH), times=3, binary_path=binary_path),
            ],
        )
        self.patch_calls = 0
        self.build_calls = 0
        self.original_source = original_source
        self.patched_source = patched_source
        self.binary_path = binary_path
        self.prepatch_observations: list[bytes] = []

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        stdin: bytes | None = None,
        env: tuple[tuple[str, str], ...] = (),
    ) -> CommandOutcome:
        if argv == ("/usr/local/bin/secb", "patch"):
            target = self.source_dir / "project" / "vulnerable.c"
            observed = target.read_bytes()
            assert observed == self.original_source
            self.prepatch_observations.append(observed)
            target.write_bytes(self.patched_source)
            self.patch_calls += 1
        elif argv == ("/usr/local/bin/secb", "build"):
            self.build_calls += 1
            binary = _workspace_path(self.source_dir, self.work_dir, self.binary_path)
            if self.build_calls == 1:
                binary.unlink(missing_ok=True)
            else:
                assert not binary.exists()
                _seed_elf(
                    binary,
                    payload=f"post-patch-build-{self.build_calls}".encode(),
                )
        return await super().run(argv, timeout=timeout, stdin=stdin, env=env)


def _cve(*, exit_code: int = 0) -> CVEInstance:
    return CVEInstance(
        instance_id="nginx.cve-2024-0001",
        repo="nginx/nginx",
        project_name="nginx",
        lang="c",
        work_dir="/work",
        sanitizer="address",
        bug_description="Heap overflow parsing headers.",
        base_commit="a" * 40,
        exit_code=exit_code,
        sanitizer_report=_ASAN_HEAP,
        bug_report=_ASAN_HEAP,
    )


def _executor(session: FakeSession | None) -> SecBenchProcedureExecutor:
    return SecBenchProcedureExecutor(lambda _root_id: session)


def _crash(output: str) -> CommandOutcome:
    return CommandOutcome(exit_code=1, output=output)


def _clean(output: str) -> CommandOutcome:
    return CommandOutcome(exit_code=0, output=output)


def _repro(
    outcome: CommandOutcome,
    times: int = 3,
    *,
    binary_path: str = "/work/bin/app",
) -> list[_Resp]:
    return [
        _Resp(f"ASAN_OPTIONS=help=1 {binary_path}", _clean(_ASAN_HELP)),
        *[
            _Resp(
                f"ASAN_OPTIONS=abort_on_error=1:halt_on_error=1 {binary_path}",
                outcome,
            )
            for _ in range(times)
        ],
    ]


def _direct_replay(
    outcome: CommandOutcome,
    times: int = 1,
    *,
    binary_path: str = "/work/bin/app",
) -> list[_Resp]:
    return [
        _Resp(f"ASAN_OPTIONS=abort_on_error=1:halt_on_error=1 {binary_path}", outcome)
        for _ in range(times)
    ]


def _binary_validation(binary_path: str = "/work/bin/app") -> list[_Resp]:
    return [
        _Resp(f"ASAN_OPTIONS=help=1 {binary_path}", _clean(_ASAN_HELP)),
    ]


def _seed_elf(path: Path, *, payload: bytes = b"fixture") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + payload)
    path.chmod(0o755)


def _workspace_path(source_dir: Path, work_dir: Path, container_path: str) -> Path:
    path = Path(container_path)
    if path.parts[:2] == ("/", "src"):
        return source_dir.joinpath(*path.parts[2:])
    if path.parts[:2] == ("/", "work"):
        return work_dir.joinpath(*path.parts[2:])
    raise ValueError(f"unsupported test workspace path: {container_path}")


def _git(worktree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=worktree,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _seed_builder_baseline(
    tc: Path,
    *,
    source_dir: Path | None = None,
    work_dir: Path | None = None,
    cve: CVEInstance | None = None,
) -> None:
    source_dir = source_dir or tc.parent / "src"
    work_dir = work_dir or tc.parent / "work"
    cve = cve or _cve()
    source_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    (tc / "base_commit_hash").write_text(cve.base_commit + "\n", encoding="utf-8")
    (tc / "repo_changes.diff").write_text("", encoding="utf-8")
    (source_dir / "build.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")


def _seed_exploit_inputs(
    tc: Path,
    *,
    repro: str = _REAL_REPRO,
    source_dir: Path | None = None,
    work_dir: Path | None = None,
    cve: CVEInstance | None = None,
    binary_path: str = "/work/bin/app",
) -> None:
    cve = cve or _cve()
    source_dir = source_dir or tc.parent / "src"
    work_dir = work_dir or tc.parent / "work"
    tc.mkdir(parents=True, exist_ok=True)
    (tc / "repro.sh").write_text(repro)
    (tc / "poc_path.txt").write_text("/testcase/poc.bin\n")
    (tc / "poc.bin").write_bytes(b"proof")
    (tc / "binary_paths.txt").write_text(binary_path + "\n")
    _seed_elf(_workspace_path(source_dir, work_dir, binary_path))
    _seed_builder_baseline(tc, source_dir=source_dir, work_dir=work_dir, cve=cve)


def _seed_faad_exploit_inputs(
    tmp_path: Path,
) -> tuple[Path, Path, Path, CVEInstance]:
    source_dir = tmp_path / "src"
    worktree = source_dir / "faad2"
    worktree.mkdir(parents=True)
    _git(worktree, "init")
    source = worktree / "decoder.c"
    source.write_text("int decode(void) { return 0; }\n", encoding="utf-8")
    target = worktree / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    _git(worktree, "add", "decoder.c", "vulnerable.c")
    _git(
        worktree,
        "-c",
        "user.name=Procedure Tests",
        "-c",
        "user.email=procedure-tests@example.invalid",
        "commit",
        "-m",
        "Seed fixture",
    )
    cve = _cve().model_copy(
        update={
            "base_commit": _git(worktree, "rev-parse", "HEAD"),
            "work_dir": "/src/faad2",
        }
    )
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(
        tc,
        source_dir=source_dir,
        cve=cve,
        binary_path=_FAAD_BINARY,
    )
    return tc, source_dir, worktree, cve


def _seed_faad_library(directory: Path) -> None:
    target = directory / "libfaad.so.2.0.0"
    _seed_elf(target)
    (directory / "libfaad.so.2").symlink_to(target.name)


async def _freeze_faad_identity(
    tc: Path,
    source_dir: Path,
    cve: CVEInstance,
) -> str:
    session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp(
                f"ASAN_OPTIONS=help=1 {_FAAD_BINARY}",
                CommandOutcome(exit_code=127, output=_FAAD_MISSING_LIBRARY),
            ),
            _Resp("/usr/bin/printenv LD_LIBRARY_PATH", _clean("/opt/runtime/lib\n")),
            _Resp(
                f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                _clean(_ASAN_HELP),
            ),
            *[
                _Resp(
                    f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                    _crash(_ASAN_HEAP),
                )
                for _ in range(3)
            ],
        ],
    )
    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator] validate", cve, {"root_id": uuid4()}
    )
    assert result.success is True
    return (tc / "exploit_input_identity.txt").read_text(encoding="utf-8").strip()


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
    assert ex.match("[Patch-Applier] apply the plan", cve) == "secb_patch_apply"
    assert ex.match("[Exploit-Validator] validate the repro", cve) == "secb_exploit_validation"
    assert ex.match("[Patch-Validator] validate the patch", cve) == "secb_patch_validation"


async def test_build_validation_passes_for_executable_elf_with_sanitizer_runtime(
    tmp_path: Path,
) -> None:
    # Given: the build succeeds and the declared executable initializes ASan
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(
        tc,
        source_dir=tmp_path / "src",
        work_dir=tmp_path / "work",
        cve=_cve(),
    )
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp("ASAN_OPTIONS=help=1 /work/target", _clean(_ASAN_HELP)),
        ],
    )
    ex = _executor(session)

    # When: the build-validation procedure runs
    result = await ex.execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    # Then: every part of the host-owned binary contract is evidenced
    assert result.success is True
    assert any(command.endswith("secb build") for command in session.commands)
    assert result.evidence[1].argv == ("host-builder-baseline-check", "/work")
    assert result.evidence[2].argv == ("host-elf-check", "/work/target")
    assert any("ASAN_OPTIONS=help=1 /work/target" in command for command in session.commands)
    assert result.evidence[-1].argv == ("host-builder-baseline-recheck", "/work")
    assert len(result.evidence) == 6


async def test_build_validation_fails_when_build_fails(tmp_path: Path) -> None:
    # Given: a session where secb build returns a nonzero exit
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    session = FakeSession(
        tc,
        responses=[
            _Resp(
                needle="secb build",
                outcome=CommandOutcome(exit_code=1, output="err"),
            )
        ],
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
    _seed_builder_baseline(tc)
    session = FakeSession(tc)  # build ok, but binary_paths.txt absent
    ex = _executor(session)

    # When: the build-validation procedure runs
    result = await ex.execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    # Then: it fails — a build without a resolvable binary is not a valid Builder outcome
    assert result.success is False


async def test_build_validation_rejects_testcase_binary_snapshot(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/testcase/bin/app\n", encoding="utf-8")
    _seed_elf(tc / "bin" / "app")
    _seed_builder_baseline(tc)
    session = FakeSession(
        tc,
        responses=[_Resp("secb build", _clean("built"))],
    )

    result = await _executor(session).execute(
        "secb_build_validation",
        "[Build-Verifier] verify",
        _cve(),
        {"root_id": uuid4()},
    )

    assert result.success is False
    assert "rebuildable /src or /work path" in result.summary


async def test_build_validation_rejects_non_elf_nonempty_target(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/not-elf\n")
    target = tmp_path / "work" / "not-elf"
    target.parent.mkdir(parents=True)
    target.write_text("not an ELF file", encoding="utf-8")
    target.chmod(0o755)
    _seed_builder_baseline(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "not a non-empty executable ELF" in result.summary
    assert not any("ASAN_OPTIONS" in command for command in session.commands)


async def test_build_validation_rejects_symlinked_binary(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    outside = tmp_path / "outside-elf"
    _seed_elf(outside)
    linked = tmp_path / "work" / "target"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(outside)
    _seed_builder_baseline(tc)
    session = FakeSession(tc, responses=[_Resp("secb build", _clean("built"))])

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "does not resolve inside the run workspace" in result.summary
    assert result.evidence[-1].argv == ("host-elf-check", "/work/target")
    assert not any("ASAN_OPTIONS" in command for command in session.commands)


async def test_build_validation_rejects_missing_sanitizer_runtime(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp(
                "ASAN_OPTIONS=help=1 /work/target",
                CommandOutcome(
                    exit_code=1,
                    output="libclang_rt.asan.so: cannot open shared object file",
                ),
            ),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "did not initialize the address sanitizer runtime" in result.summary
    assert result.digest is not None
    assert "libclang_rt.asan.so" in result.digest
    assert not any(
        invocation[0] == ("/usr/bin/printenv", "LD_LIBRARY_PATH")
        for invocation in session.invocations
    )


async def test_build_validation_checks_every_declared_binary(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/first\n/work/second\n")
    _seed_elf(tmp_path / "work" / "first")
    second = tmp_path / "work" / "second"
    second.write_text("not ELF", encoding="utf-8")
    second.chmod(0o755)
    _seed_builder_baseline(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp("ASAN_OPTIONS=help=1 /work/first", _clean(_ASAN_HELP)),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "/work/second" in result.summary
    assert not any("ASAN_OPTIONS=help=1 /work/first" in command for command in session.commands)
    assert not any("ASAN_OPTIONS=help=1 /work/second" in command for command in session.commands)


@pytest.mark.parametrize(
    ("sanitizer", "environment", "marker"),
    [
        ("address", "ASAN_OPTIONS", "Available flags for AddressSanitizer:"),
        ("memory", "MSAN_OPTIONS", "Available flags for MemorySanitizer:"),
        ("undefined", "UBSAN_OPTIONS", "Available flags for UndefinedBehaviorSanitizer:"),
    ],
)
async def test_build_validation_probes_configured_sanitizer(
    tmp_path: Path,
    sanitizer: str,
    environment: str,
    marker: str,
) -> None:
    tc = tmp_path / sanitizer
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    cve = _cve().model_copy(update={"sanitizer": sanitizer})
    _seed_builder_baseline(
        tc,
        source_dir=tmp_path / "src",
        work_dir=tmp_path / "work",
        cve=cve,
    )
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp(f"{environment}=help=1 /work/target", _clean(marker)),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation",
        "[Build-Verifier] verify",
        cve,
        {"root_id": uuid4()},
    )

    assert result.success is True
    probe_evidence = next(
        item for item in result.evidence if item.argv == ("/work/target", "--help")
    )
    assert marker in probe_evidence.excerpt


async def test_build_validation_stops_probe_after_sanitizer_marker(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(
        tc,
        source_dir=tmp_path / "src",
        work_dir=tmp_path / "work",
        cve=_cve(),
    )
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp(
                "ASAN_OPTIONS=help=1 /work/target",
                CommandOutcome(
                    exit_code=143,
                    output=_ASAN_HELP,
                    termination=CommandTermination.STOPPED_AFTER_MARKER,
                ),
            ),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is True
    assert session.marker_invocations == [
        (
            ("/work/target", "--help"),
            _ASAN_HELP,
            (("ASAN_OPTIONS", "help=1"),),
            10.0,
        )
    ]
    marker_evidence = next(
        item for item in result.evidence if item.argv[0] == "host-sanitizer-startup-marker"
    )
    assert "termination=stopped_after_marker" in marker_evidence.excerpt


async def test_build_validation_rejects_probe_timeout_without_sanitizer_marker(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp(
                "ASAN_OPTIONS=help=1 /work/target",
                CommandOutcome(
                    exit_code=124,
                    output="generating lookup tables",
                    termination=CommandTermination.TIMED_OUT,
                ),
            ),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "sanitizer probe timed out" in result.summary


async def test_build_validation_rejects_marker_reported_as_timeout(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp(
                "ASAN_OPTIONS=help=1 /work/target",
                CommandOutcome(
                    exit_code=124,
                    output=_ASAN_HELP,
                    termination=CommandTermination.TIMED_OUT,
                ),
            ),
        ],
    )

    with pytest.raises(
        ProcedureInfrastructureError,
        match="preserved its success marker without stopping",
    ):
        await _executor(session).execute(
            "secb_build_validation",
            "[Build-Verifier] verify",
            _cve(),
            {"root_id": uuid4()},
        )


async def test_build_validation_rechecks_baseline_after_marker_stop(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(tc)

    class _ProbeMutatingSession(FakeSession):
        async def run_until_marker(
            self,
            argv: tuple[str, ...],
            *,
            marker: str,
            timeout: float,
            env: tuple[tuple[str, str], ...] = (),
        ) -> CommandOutcome:
            outcome = await super().run_until_marker(
                argv,
                marker=marker,
                timeout=timeout,
                env=env,
            )
            (self.source_dir / "build.sh").write_text(
                "#!/bin/bash\nexit 17\n",
                encoding="utf-8",
            )
            return outcome

    session = _ProbeMutatingSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp(
                "ASAN_OPTIONS=help=1 /work/target",
                CommandOutcome(
                    exit_code=143,
                    output=_ASAN_HELP,
                    termination=CommandTermination.STOPPED_AFTER_MARKER,
                ),
            ),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation",
        "[Build-Verifier] verify",
        _cve(),
        {"root_id": uuid4()},
    )

    assert result.success is False
    assert "baseline changed during the sanitizer startup probe" in result.summary
    assert result.evidence[-1].argv == ("host-builder-baseline-recheck", "/work")


async def test_build_validation_rejects_binary_pointer_mutated_by_probe(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n", encoding="utf-8")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(tc)

    class _PointerMutatingSession(FakeSession):
        async def run_until_marker(
            self,
            argv: tuple[str, ...],
            *,
            marker: str,
            timeout: float,
            env: tuple[tuple[str, str], ...] = (),
        ) -> CommandOutcome:
            outcome = await super().run_until_marker(
                argv,
                marker=marker,
                timeout=timeout,
                env=env,
            )
            (self.testcase_dir / "binary_paths.txt").write_text(
                "/work/target\n\n",
                encoding="utf-8",
            )
            return outcome

    session = _PointerMutatingSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp("ASAN_OPTIONS=help=1 /work/target", _clean(_ASAN_HELP)),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "binary pointer changed during the sanitizer startup probe" in result.summary


async def test_build_validation_rejects_binary_mutated_by_probe(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/work/target\n", encoding="utf-8")
    _seed_elf(tmp_path / "work" / "target")
    _seed_builder_baseline(tc)

    class _BinaryMutatingSession(FakeSession):
        async def run_until_marker(
            self,
            argv: tuple[str, ...],
            *,
            marker: str,
            timeout: float,
            env: tuple[tuple[str, str], ...] = (),
        ) -> CommandOutcome:
            outcome = await super().run_until_marker(
                argv,
                marker=marker,
                timeout=timeout,
                env=env,
            )
            _seed_elf(self.work_dir / "target", payload=b"mutated-by-probe")
            return outcome

    session = _BinaryMutatingSession(
        tc,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp("ASAN_OPTIONS=help=1 /work/target", _clean(_ASAN_HELP)),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "declared Builder binary changed during the sanitizer startup probe" in result.summary


@pytest.mark.parametrize(
    ("index_flag", "expected_index_prefix"),
    [
        ("--assume-unchanged", "h "),
        ("--skip-worktree", "S "),
    ],
)
async def test_build_validation_rejects_worker_index_hidden_tracked_drift(
    tmp_path: Path,
    index_flag: str,
    expected_index_prefix: str,
) -> None:
    source_dir = tmp_path / "src"
    worktree = source_dir / "project"
    worktree.mkdir(parents=True)
    _git(worktree, "init")
    source = worktree / "source.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _git(worktree, "add", "source.c")
    _git(
        worktree,
        "-c",
        "user.name=Procedure Tests",
        "-c",
        "user.email=procedure-tests@example.invalid",
        "commit",
        "-m",
        "Seed fixture",
    )
    base_commit = _git(worktree, "rev-parse", "HEAD")
    cve = _cve().model_copy(
        update={"base_commit": base_commit, "work_dir": "/src/project"}
    )
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text(
        "/src/project/bin/app\n",
        encoding="utf-8",
    )
    _seed_elf(worktree / "bin" / "app")
    _seed_builder_baseline(tc, source_dir=source_dir, cve=cve)
    _git(worktree, "update-index", index_flag, "source.c")
    source.write_text("int main(void) { return 17; }\n", encoding="utf-8")
    session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[_Resp("secb build", _clean("built"))],
    )

    result = await _executor(session).execute(
        "secb_build_validation",
        "[Build-Verifier] verify",
        cve,
        {"root_id": uuid4()},
    )

    assert result.success is False
    assert "tracked source or build inputs changed" in result.summary
    assert _git(worktree, "ls-files", "-v", "source.c").startswith(expected_index_prefix)
    assert session.marker_invocations == []


async def test_build_validation_rejects_repo_diff_that_does_not_match_worktree(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "src"
    worktree = source_dir / "project"
    worktree.mkdir(parents=True)
    _git(worktree, "init")
    source = worktree / "source.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    _git(worktree, "add", "source.c")
    _git(
        worktree,
        "-c",
        "user.name=Procedure Tests",
        "-c",
        "user.email=procedure-tests@example.invalid",
        "commit",
        "-m",
        "Seed fixture",
    )
    base_commit = _git(worktree, "rev-parse", "HEAD")
    cve = _cve().model_copy(
        update={"base_commit": base_commit, "work_dir": "/src/project"}
    )
    tc = tmp_path / "testcase"
    tc.mkdir()
    (tc / "binary_paths.txt").write_text("/src/project/bin/app\n", encoding="utf-8")
    _seed_elf(worktree / "bin" / "app")
    (tc / "base_commit_hash").write_text(base_commit + "\n", encoding="utf-8")
    (tc / "repo_changes.diff").write_bytes(b"\n")
    (source_dir / "build.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp("secb build", _clean("built")),
            _Resp("ASAN_OPTIONS=help=1 /src/project/bin/app", _clean(_ASAN_HELP)),
        ],
    )

    result = await _executor(session).execute(
        "secb_build_validation", "[Build-Verifier] verify", cve, {"root_id": uuid4()}
    )

    assert result.success is False
    assert "Builder commit does not match repo_changes.diff" in result.summary
    assert not any("ASAN_OPTIONS" in command for command in session.commands)


def _patch_plan(target: Path) -> dict:
    return {
        "evidence_references": ["/testcase/root_cause_analysis.txt"],
        "target_file": "/src/project/vulnerable.c",
        "target_symbol": "parse",
        "base_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "operations": [
            {
                "kind": "replace",
                "anchor": "unsafe();",
                "replacement": "safe();",
                "expected_occurrences": 1,
            }
        ],
        "allowed_paths": ["/src/project"],
        "forbidden_paths": ["/testcase"],
        "required_postconditions": ["sanitizer finding absent"],
        "validation_commands": ["secb patch", "secb build", "secb repro"],
    }


async def _freeze_exploit_identity(
    tc: Path,
    *,
    cve: CVEInstance | None = None,
    source_dir: Path | None = None,
    work_dir: Path | None = None,
    binary_path: str = "/work/bin/app",
) -> str:
    cve = cve or _cve()
    source_dir = source_dir or tc.parent / "src"
    work_dir = work_dir or tc.parent / "work"
    _seed_exploit_inputs(
        tc,
        cve=cve,
        source_dir=source_dir,
        work_dir=work_dir,
        binary_path=binary_path,
    )
    session = FakeSession(
        tc,
        responses=_repro(_crash(_ASAN_HEAP), binary_path=binary_path),
        source_dir=source_dir,
        work_dir=work_dir,
    )
    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator] verify", cve, {"root_id": uuid4()}
    )
    assert result.success is True
    return (tc / "exploit_input_identity.txt").read_text(encoding="utf-8").strip()


async def _seed_patch_inputs(
    tc: Path,
    *,
    cve: CVEInstance | None = None,
    target_bytes: bytes = b"void parse(void) { unsafe(); }\n",
    binary_path: str = "/work/bin/app",
) -> None:
    cve = cve or _cve()
    source_dir = tc.parent / "src"
    target = source_dir / "project" / "vulnerable.c"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(target_bytes)
    await _freeze_exploit_identity(
        tc,
        cve=cve,
        source_dir=source_dir,
        binary_path=binary_path,
    )
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )
    session = FakeSession(tc, source_dir=source_dir)
    result = await _executor(session).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )
    assert result.success is True


async def test_patch_apply_applies_validated_plan_and_writes_diff(tmp_path: Path) -> None:
    # Given: a sealed /src source and a solver-authored PatchPlan with real evidence
    src = tmp_path / "src" / "project"
    src.mkdir(parents=True)
    target = src / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    tc = tmp_path / "testcase"
    identity = await _freeze_exploit_identity(tc, source_dir=tmp_path / "src")
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )
    victim = tmp_path / "model-patch-victim"
    victim.write_text("unchanged\n", encoding="utf-8")
    (tc / "model_patch.diff").symlink_to(victim)
    session = FakeSession(tc, source_dir=tmp_path / "src")

    # When: the weak Patch-Applier procedure runs
    result = await _executor(session).execute(
        "secb_patch_apply", "[Patch-Applier] apply", _cve(), {"root_id": uuid4()}
    )

    # Then: source remains fresh and the exact edit is privately bound to replay identity
    assert result.success is True
    assert target.read_text(encoding="utf-8") == "void parse(void) { unsafe(); }\n"
    rendered = (tc / "model_patch.diff").read_text(encoding="utf-8")
    assert rendered.startswith("diff --git a/project/vulnerable.c b/project/vulnerable.c")
    assert "+void parse(void) { safe(); }" in rendered
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert not (tc / "model_patch.diff").is_symlink()
    assert result.plan_approval is not None
    private_approval = json.loads(
        (session.identity_dir / "patch-approval.json").read_text(encoding="utf-8")
    )
    assert private_approval["replay_identity"] == identity


async def test_patch_apply_blocks_and_makes_no_edit_on_forbidden_target(tmp_path: Path) -> None:
    # Given: a PatchPlan that forbids its own target path
    src = tmp_path / "src" / "project"
    src.mkdir(parents=True)
    target = src / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    before = target.read_bytes()
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, source_dir=tmp_path / "src")
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    plan = _patch_plan(target)
    plan["forbidden_paths"] = ["/src/project"]
    (tc / "patch_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    session = FakeSession(tc, source_dir=tmp_path / "src")

    # When: the applier validates the plan
    result = await _executor(session).execute(
        "secb_patch_apply", "[Patch-Applier] apply", _cve(), {"root_id": uuid4()}
    )

    # Then: it blocks, edits nothing, and emits no diff deliverable
    assert result.success is False
    assert target.read_bytes() == before
    assert not (tc / "model_patch.diff").exists()


async def test_patch_apply_blocks_unresolved_evidence_reference(tmp_path: Path) -> None:
    # Given: A structurally valid plan citing an evidence file that does not exist
    src = tmp_path / "src" / "project"
    src.mkdir(parents=True)
    target = src / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, source_dir=tmp_path / "src")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )
    session = FakeSession(tc, source_dir=tmp_path / "src")

    # When: The host validates the plan evidence
    result = await _executor(session).execute(
        "secb_patch_apply", "[Patch-Applier] apply", _cve(), {"root_id": uuid4()}
    )

    # Then: The plan blocks and source is untouched
    assert result.success is False
    assert "unresolved evidence" in result.summary
    assert target.read_text(encoding="utf-8") == "void parse(void) { unsafe(); }\n"


async def test_patch_apply_refuses_symlinked_plan_without_disclosure(tmp_path: Path) -> None:
    src = tmp_path / "src" / "project"
    src.mkdir(parents=True)
    target = src / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, source_dir=tmp_path / "src")
    secret = "DO-NOT-DISCLOSE-patch-plan"
    outside = tmp_path / "outside-plan.json"
    outside.write_text(secret, encoding="utf-8")
    (tc / "patch_plan.json").symlink_to(outside)

    result = await _executor(FakeSession(tc, source_dir=tmp_path / "src")).execute(
        "secb_patch_apply", "[Patch-Applier] apply", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert secret not in result.summary
    assert result.digest is not None
    assert secret not in result.digest
    assert target.read_text(encoding="utf-8") == "void parse(void) { unsafe(); }\n"


async def test_patch_apply_rejects_binary_changed_after_exploit_freeze(tmp_path: Path) -> None:
    src = tmp_path / "src" / "project"
    src.mkdir(parents=True)
    target = src / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, source_dir=tmp_path / "src")
    _seed_elf(tmp_path / "work" / "bin" / "app", payload=b"changed-after-freeze")
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )

    result = await _executor(FakeSession(tc, source_dir=tmp_path / "src")).execute(
        "secb_patch_apply", "[Patch-Applier] apply", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "target identity changed" in result.summary
    assert not (tc / "model_patch.diff").exists()


async def test_patch_apply_rejects_loader_dependency_changed_after_exploit_freeze(
    tmp_path: Path,
) -> None:
    tc, source_dir, worktree, cve = _seed_faad_exploit_inputs(tmp_path)
    library_dir = worktree / "libfaad" / ".libs"
    _seed_faad_library(library_dir)
    await _freeze_faad_identity(tc, source_dir, cve)
    _seed_elf(library_dir / "libfaad.so.2.0.0", payload=b"changed-after-freeze")

    target = worktree / "vulnerable.c"
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    plan = _patch_plan(target)
    plan["target_file"] = "/src/faad2/vulnerable.c"
    plan["allowed_paths"] = ["/src/faad2"]
    (tc / "patch_plan.json").write_text(json.dumps(plan), encoding="utf-8")

    result = await _executor(FakeSession(tc, source_dir=source_dir)).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )

    assert result.success is False
    assert "project library content changed after Host validation" in result.summary
    assert not (tc / "model_patch.diff").exists()


async def test_patch_apply_rejects_build_input_changed_after_exploit_freeze(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src" / "project"
    src.mkdir(parents=True)
    target = src / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, source_dir=tmp_path / "src")
    (tmp_path / "src" / "build.sh").write_text(
        "#!/bin/bash\necho changed\n", encoding="utf-8"
    )
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )

    result = await _executor(FakeSession(tc, source_dir=tmp_path / "src")).execute(
        "secb_patch_apply", "[Patch-Applier] apply", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "target identity changed" in result.summary
    assert not (tc / "model_patch.diff").exists()


async def test_patch_apply_rejects_tracked_source_changed_after_exploit_freeze(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "src"
    worktree = source_dir / "project"
    worktree.mkdir(parents=True)
    _git(worktree, "init")
    target = worktree / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    _git(worktree, "add", "vulnerable.c")
    _git(
        worktree,
        "-c",
        "user.name=Procedure Tests",
        "-c",
        "user.email=procedure-tests@example.invalid",
        "commit",
        "-m",
        "Seed fixture",
    )
    base_commit = _git(worktree, "rev-parse", "HEAD")
    cve = _cve().model_copy(
        update={"base_commit": base_commit, "work_dir": "/src/project"}
    )
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, cve=cve, source_dir=source_dir)
    target.write_text("void parse(void) { unsafe(); }\n// drift\n", encoding="utf-8")
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )

    result = await _executor(FakeSession(tc, source_dir=source_dir)).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )

    assert result.success is False
    assert "tracked source or build inputs changed" in result.summary
    assert not (tc / "model_patch.diff").exists()


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
    assert ex.resolve("secb_patch_apply") is True
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


async def test_exploit_validation_rejects_pointer_mutated_during_replay(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)

    class _ReplayMutatingSession(FakeSession):
        mutated = False

        async def run(
            self,
            argv: tuple[str, ...],
            *,
            timeout: float,
            stdin: bytes | None = None,
            env: tuple[tuple[str, str], ...] = (),
        ) -> CommandOutcome:
            outcome = await super().run(argv, timeout=timeout, stdin=stdin, env=env)
            if argv == ("/work/bin/app",) and stdin == b"proof" and not self.mutated:
                (self.testcase_dir / "binary_paths.txt").write_text(
                    "/work/bin/app\n\n",
                    encoding="utf-8",
                )
                self.mutated = True
            return outcome

    session = _ReplayMutatingSession(tc, responses=_repro(_crash(_ASAN_HEAP)))

    result = await _executor(session).execute(
        "secb_exploit_validation",
        "[Exploit-Validator] validate",
        _cve(),
        {"root_id": uuid4()},
    )

    assert result.success is False
    assert "binary pointer changed after exploit validation" in result.summary
    assert "VERDICT: FAIL" in (tc / "exploit_validation_results.txt").read_text()
    assert not (tc / "exploit_input_identity.txt").exists()
    assert not (session.identity_dir / "replay-contract.json").exists()


async def test_exploit_validation_freezes_project_loader_path_for_faad(
    tmp_path: Path,
) -> None:
    tc, source_dir, worktree, cve = _seed_faad_exploit_inputs(tmp_path)
    _seed_faad_library(worktree / "libfaad" / ".libs")
    session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp(
                f"ASAN_OPTIONS=help=1 {_FAAD_BINARY}",
                CommandOutcome(exit_code=127, output=_FAAD_MISSING_LIBRARY),
            ),
            _Resp("/usr/bin/printenv LD_LIBRARY_PATH", _clean("/opt/runtime/lib\n")),
            _Resp(
                f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                _clean(_ASAN_HELP),
            ),
            *[
                _Resp(
                    f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                    _crash(_ASAN_HEAP),
                )
                for _ in range(3)
            ],
        ],
    )

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator] validate", cve, {"root_id": uuid4()}
    )

    assert result.success is True
    binary_invocations = [
        invocation for invocation in session.invocations if invocation[0][0] == _FAAD_BINARY
    ]
    assert binary_invocations[0][2] == (("ASAN_OPTIONS", "help=1"),)
    assert binary_invocations[1][2] == (
        ("ASAN_OPTIONS", "help=1"),
        ("LD_LIBRARY_PATH", _FAAD_LOADER_PATH),
    )
    replays = [
        invocation
        for invocation in binary_invocations
        if invocation[0] == (_FAAD_BINARY,)
    ]
    assert len(replays) == 3
    assert all(
        ("LD_LIBRARY_PATH", _FAAD_LOADER_PATH) in invocation[2]
        for invocation in replays
    )
    contract = json.loads((session.identity_dir / "replay-contract.json").read_text())
    assert contract["schema_version"] == "3"
    binding = contract["binary_bindings"][0]
    assert binding["path"] == _FAAD_BINARY
    assert binding["mode"] == 0o755
    assert len(binding["sha256"]) == 64
    assert binding["loader"] == {
        "missing_soname": "libfaad.so.2",
        "candidate_path": "/src/faad2/libfaad/.libs/libfaad.so.2",
        "resolved_target_path": "/src/faad2/libfaad/.libs/libfaad.so.2.0.0",
        "resolved_target_sha256": hashlib.sha256(
            (worktree / "libfaad" / ".libs" / "libfaad.so.2.0.0").read_bytes()
        ).hexdigest(),
        "effective_loader_path": _FAAD_LOADER_PATH,
    }


async def test_exploit_validation_rejects_ambiguous_project_loader_library(
    tmp_path: Path,
) -> None:
    tc, source_dir, worktree, cve = _seed_faad_exploit_inputs(tmp_path)
    _seed_faad_library(worktree / "libfaad" / ".libs")
    _seed_faad_library(worktree / "duplicate" / ".libs")
    session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp(
                f"ASAN_OPTIONS=help=1 {_FAAD_BINARY}",
                CommandOutcome(exit_code=127, output=_FAAD_MISSING_LIBRARY),
            ),
        ],
    )

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator] validate", cve, {"root_id": uuid4()}
    )

    assert result.success is False
    assert "multiple project-local libfaad.so.2 candidates" in result.summary
    assert not any(
        invocation[0] == ("/usr/bin/printenv", "LD_LIBRARY_PATH")
        for invocation in session.invocations
    )
    assert sum(invocation[0][0] == _FAAD_BINARY for invocation in session.invocations) == 1


async def test_exploit_validation_rejects_project_loader_symlink_escape(
    tmp_path: Path,
) -> None:
    tc, source_dir, worktree, cve = _seed_faad_exploit_inputs(tmp_path)
    outside = tmp_path / "outside" / "libfaad.so.2.0.0"
    _seed_elf(outside)
    library_dir = worktree / "libfaad" / ".libs"
    library_dir.mkdir(parents=True)
    (library_dir / "libfaad.so.2").symlink_to(outside)
    session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp(
                f"ASAN_OPTIONS=help=1 {_FAAD_BINARY}",
                CommandOutcome(exit_code=127, output=_FAAD_MISSING_LIBRARY),
            ),
        ],
    )

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator] validate", cve, {"root_id": uuid4()}
    )

    assert result.success is False
    assert "project-local libfaad.so.2 is unsafe" in result.summary
    assert "resolved target escapes" in result.summary
    assert not any(
        invocation[0] == ("/usr/bin/printenv", "LD_LIBRARY_PATH")
        for invocation in session.invocations
    )
    assert sum(invocation[0][0] == _FAAD_BINARY for invocation in session.invocations) == 1


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
            *_binary_validation(),
            *_direct_replay(_crash(_ASAN_HEAP), times=2),
            *_direct_replay(_clean(_NO_CRASH)),
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


@pytest.mark.parametrize(
    "exit_codes",
    [(1, 2, 1), (-11, -6, -11)],
    ids=["normal-exits", "signals"],
)
async def test_exploit_validation_inconsistent_terminations_fail(
    tmp_path: Path, exit_codes: tuple[int, int, int]
) -> None:
    # Given: three identical oracle crash signatures with inconsistent exits or signals
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            *_binary_validation(),
            *[
                _Resp(
                    "ASAN_OPTIONS=abort_on_error=1:halt_on_error=1 /work/bin/app",
                    CommandOutcome(exit_code=exit_code, output=_ASAN_HEAP),
                )
                for exit_code in exit_codes
            ],
        ],
    )
    ex = _executor(session)

    # When: the exploit-validation procedure evaluates the replay trio
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: matching crash text cannot pass with inconsistent process termination
    assert result.success is False
    assert "exit codes/signals are inconsistent" in result.summary
    assert "VERDICT: FAIL" in (tc / "exploit_validation_results.txt").read_text()


async def test_exploit_validation_accepts_complete_signature_with_clean_exit(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(
        tc,
        responses=_repro(CommandOutcome(exit_code=0, output=_ASAN_HEAP)),
    )

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is True
    assert "matches the CVE oracle across 3/3 deterministic runs" in result.summary


@pytest.mark.parametrize(
    ("final_command", "expected_argv"),
    [
        (
            'exec "$BIN" -v "$POC" /dev/null',
            ("/work/bin/app", "-v", "/testcase/poc.bin", "/dev/null"),
        ),
        (
            'exec "$BIN" "$POC" -o /dev/null',
            ("/work/bin/app", "/testcase/poc.bin", "-o", "/dev/null"),
        ),
    ],
    ids=["openexr", "faad2"],
)
async def test_exploit_validation_executes_structured_replay_argv(
    tmp_path: Path,
    final_command: str,
    expected_argv: tuple[str, ...],
) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc, repro=_repro_script(final_command))
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is True
    replays = [
        call
        for call in session.invocations
        if call[0][0] == "/work/bin/app" and call[0][-1] != "--help"
    ]
    assert [call[0] for call in replays] == [expected_argv] * 3
    assert all(call[1] is None for call in replays)
    assert all(
        call[2] == (("ASAN_OPTIONS", "abort_on_error=1:halt_on_error=1"),)
        for call in replays
    )


async def test_exploit_validation_passes_poc_bytes_as_direct_stdin(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    poc = b"\x00proof\xff"
    (tc / "poc.bin").write_bytes(poc)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is True
    replays = [call for call in session.invocations if call[0] == ("/work/bin/app",)]
    assert len(replays) == 3
    assert all(call[1] == poc for call in replays)


@pytest.mark.parametrize(
    "final_command",
    [
        'exec "$BIN" "$POC"; echo forged',
        'exec "$BIN" "$POC" | cat',
        'exec "$BIN" "$POC" && true',
        'exec "$BIN" "$(cat /testcase/poc_path.txt)"',
        'exec "$BIN" `cat /testcase/poc_path.txt`',
        'exec "$BIN" "$POC" > /dev/null',
        'exec "$BIN" "$POC" "$POC"',
    ],
    ids=[
        "semicolon",
        "pipe",
        "and",
        "substitution",
        "backticks",
        "redirect",
        "duplicate-poc",
    ],
)
async def test_exploit_validation_rejects_unsafe_replay_grammar(
    tmp_path: Path,
    final_command: str,
) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc, repro=_repro_script(final_command))
    session = FakeSession(tc)

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "not a Host-verifiable direct replay" in result.summary
    assert session.invocations == []


async def test_exploit_validation_rejects_forged_echo_and_exit_script(tmp_path: Path) -> None:
    forged = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "# /testcase/binary_paths.txt /testcase/poc_path.txt\n"
        "echo 'ERROR: AddressSanitizer: heap-buffer-overflow'\n"
        "exit 1\n"
    )
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc, repro=forged)
    session = FakeSession(tc)

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "not a Host-verifiable direct replay" in result.summary
    assert session.commands == []


@pytest.mark.parametrize(
    "artifact",
    ["repro.sh", "poc_path.txt", "binary_paths.txt"],
)
async def test_exploit_validation_refuses_symlinked_inputs_without_disclosure(
    tmp_path: Path,
    artifact: str,
) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    secret = "DO-NOT-DISCLOSE-procedure-input"
    outside = tmp_path / f"outside-{artifact}"
    outside.write_text(secret, encoding="utf-8")
    candidate = tc / artifact
    candidate.unlink()
    candidate.symlink_to(outside)
    session = FakeSession(tc)

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert secret not in result.summary
    assert result.digest is not None
    assert secret not in result.digest
    assert session.invocations == []


async def test_exploit_verdict_atomic_write_does_not_follow_symlink(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged\n", encoding="utf-8")
    verdict = tc / "exploit_validation_results.txt"
    verdict.symlink_to(victim)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is True
    assert victim.read_text(encoding="utf-8") == "unchanged\n"
    assert verdict.is_file()
    assert not verdict.is_symlink()
    assert "VERDICT: PASS" in verdict.read_text(encoding="utf-8")


async def test_exploit_validation_rejects_poc_outside_testcase(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"proof")
    (tc / "poc_path.txt").write_text(str(outside) + "\n", encoding="utf-8")
    session = FakeSession(tc)

    result = await _executor(session).execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    assert result.success is False
    assert "under /testcase" in result.summary
    assert session.commands == []


async def test_exploit_validation_accepts_zero_byte_poc_but_requires_nonempty_binary(
    tmp_path: Path,
) -> None:
    # Given: a declared zero-byte PoC and a deterministic oracle crash
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    (tc / "poc.bin").write_bytes(b"")
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))
    ex = _executor(session)

    # When: exploit preflight resolves the PoC and Builder binary
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: PoC existence is sufficient, while the binary retains the non-empty gate
    assert result.success is True
    assert result.evidence[0].argv == ("host-elf-check", "/work/bin/app")


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

    # Then: preflight fails, no direct replay is ever launched, verdict still written
    assert result.success is False
    assert not any(invocation[0][0] == "/work/bin/app" for invocation in session.invocations)
    verdict = (tc / "exploit_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "skeleton" in verdict


async def test_exploit_validation_unresolved_binary_preflight_fails(tmp_path: Path) -> None:
    # Given: the declared Builder binary does not resolve to a real file
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    (tmp_path / "work" / "bin" / "app").unlink()
    session = FakeSession(
        tc, responses=[_Resp("/work/bin/app", CommandOutcome(exit_code=1, output=""))]
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: preflight fails on the binary, replay never runs
    assert result.success is False
    assert not any(invocation[0][0] == "/work/bin/app" for invocation in session.invocations)
    assert "does not resolve inside the run workspace" in (
        tc / "exploit_validation_results.txt"
    ).read_text()


async def test_exploit_validation_repro_timeout_with_matching_crash_still_fails(
    tmp_path: Path,
) -> None:
    # Given: the first Host-bound replay times out after printing the expected crash
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    timeout_outcome = CommandOutcome(
        exit_code=124,
        output=f"{_ASAN_HEAP}\npartial repro output before the timeout",
        termination=CommandTermination.TIMED_OUT,
    )
    session = FakeSession(
        tc, responses=[*_binary_validation(), *_direct_replay(timeout_outcome)]
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: startup-marker semantics do not weaken replay deadlines
    assert result.success is False
    assert result.digest
    assert "timed out" in result.summary
    assert "partial repro output before the timeout" in result.digest
    assert session.marker_invocations == [
        (
            ("/work/bin/app", "--help"),
            _ASAN_HELP,
            (("ASAN_OPTIONS", "help=1"),),
            10.0,
        )
    ]


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
    # Given: a passing exploit run (ELF/sanitizer preflight + 3 direct replays)
    tc = tmp_path / "testcase"
    _seed_exploit_inputs(tc)
    session = FakeSession(tc, responses=_repro(_crash(_ASAN_HEAP)))
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_exploit_validation", "[Exploit-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: commands and the separate marker observation are fully evidenced
    assert len(result.evidence) == 6
    assert any(item.argv[0] == "host-sanitizer-startup-marker" for item in result.evidence)
    for item in result.evidence:
        assert item.argv
        assert len(item.output_sha256) == 64
        assert len(item.excerpt) <= 500


# ---------------------------------------------------------------------------
# Patch validation
# ---------------------------------------------------------------------------


async def test_patch_validation_rejects_raw_plan_mutation_after_approval(
    tmp_path: Path,
) -> None:
    # Given: A Host-approved plan whose raw bytes are changed without changing its JSON value
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    plan_path = tc / "patch_plan.json"
    plan_path.write_bytes(plan_path.read_bytes() + b"\n")
    session = FakeSession(tc)

    # When: Patch validation checks the private approval before running any command
    result = await _executor(session).execute(
        "secb_patch_validation", "[Patch-Validator] validate", _cve(), {"root_id": uuid4()}
    )

    # Then: Even whitespace-only raw drift invalidates the approval
    assert result.success is False
    assert "raw bytes changed after Host approval" in result.summary
    assert session.commands == []


async def test_patch_validation_pass_writes_verdict_and_logs(tmp_path: Path) -> None:
    # Given: a clean apply, a successful build, and 3/3 no-crash repro
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
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
    assert "PATCHED_FILES: project/vulnerable.c" in verdict
    assert "PRE_PATCH_SANITIZER_ERROR: heap-buffer-overflow" in verdict

    # And: the fix-loop artifacts match the on-disk contract
    fix_loop = (tc / "fix_loop.log").read_text()
    assert "patch_exit=0" in fix_loop
    assert "build_exit=0" in fix_loop
    for n in (1, 2, 3):
        assert (tc / f"fix_run_{n}.log").read_text().rstrip().endswith("exit=0")
    assert (tc / "fix_loop.exit").read_text().strip() == "done"


async def test_patch_validation_reuses_frozen_faad_loader_path(tmp_path: Path) -> None:
    tc, source_dir, worktree, cve = _seed_faad_exploit_inputs(tmp_path)
    _seed_faad_library(worktree / "libfaad" / ".libs")
    exploit_session = FakeSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp(
                f"ASAN_OPTIONS=help=1 {_FAAD_BINARY}",
                CommandOutcome(exit_code=127, output=_FAAD_MISSING_LIBRARY),
            ),
            _Resp("/usr/bin/printenv LD_LIBRARY_PATH", _clean("/opt/runtime/lib\n")),
            _Resp(
                f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                _clean(_ASAN_HELP),
            ),
            *[
                _Resp(
                    f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                    _crash(_ASAN_HEAP),
                )
                for _ in range(3)
            ],
        ],
    )
    exploit = await _executor(exploit_session).execute(
        "secb_exploit_validation", "[Exploit-Validator] validate", cve, {"root_id": uuid4()}
    )
    assert exploit.success is True

    target = worktree / "vulnerable.c"
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    plan = _patch_plan(target)
    plan["target_file"] = "/src/faad2/vulnerable.c"
    plan["allowed_paths"] = ["/src/faad2"]
    (tc / "patch_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    approval = await _executor(FakeSession(tc, source_dir=source_dir)).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )
    assert approval.success is True

    class _RebuildingFaadSession(FakeSession):
        async def run(
            self,
            argv: tuple[str, ...],
            *,
            timeout: float,
            stdin: bytes | None = None,
            env: tuple[tuple[str, str], ...] = (),
        ) -> CommandOutcome:
            if argv == ("/usr/local/bin/secb", "build"):
                _seed_elf(
                    worktree / "libfaad" / ".libs" / "libfaad.so.2.0.0",
                    payload=b"rebuilt-project-library",
                )
            return await super().run(argv, timeout=timeout, stdin=stdin, env=env)

    session = _RebuildingFaadSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("rebuilt")),
            _Resp(
                f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                _clean(_ASAN_HELP),
            ),
            *[
                _Resp(
                    f"LD_LIBRARY_PATH={_FAAD_LOADER_PATH} {_FAAD_BINARY}",
                    _clean(_NO_CRASH),
                )
                for _ in range(3)
            ],
        ],
    )

    result = await _executor(session).execute(
        "secb_patch_validation", "[Patch-Validator] validate", cve, {"root_id": uuid4()}
    )

    assert result.success is True
    binary_invocations = [
        invocation for invocation in session.invocations if invocation[0][0] == _FAAD_BINARY
    ]
    assert len(binary_invocations) == 4
    assert binary_invocations[0][0] == (_FAAD_BINARY, "--help")
    assert all(
        ("LD_LIBRARY_PATH", _FAAD_LOADER_PATH) in invocation[2]
        for invocation in binary_invocations
    )
    assert not any(
        invocation[0] == ("/usr/bin/printenv", "LD_LIBRARY_PATH")
        for invocation in session.invocations
    )


async def test_patch_validation_rejects_rebuilt_loader_symlink_escape(
    tmp_path: Path,
) -> None:
    tc, source_dir, worktree, cve = _seed_faad_exploit_inputs(tmp_path)
    library_dir = worktree / "libfaad" / ".libs"
    _seed_faad_library(library_dir)
    await _freeze_faad_identity(tc, source_dir, cve)

    target = worktree / "vulnerable.c"
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    plan = _patch_plan(target)
    plan["target_file"] = "/src/faad2/vulnerable.c"
    plan["allowed_paths"] = ["/src/faad2"]
    (tc / "patch_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    approval = await _executor(FakeSession(tc, source_dir=source_dir)).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )
    assert approval.success is True

    outside = tmp_path / "outside" / "libfaad.so.2.0.0"
    _seed_elf(outside, payload=b"outside-loader")

    class _EscapingRebuildSession(FakeSession):
        async def run(
            self,
            argv: tuple[str, ...],
            *,
            timeout: float,
            stdin: bytes | None = None,
            env: tuple[tuple[str, str], ...] = (),
        ) -> CommandOutcome:
            if argv == ("/usr/local/bin/secb", "build"):
                soname = library_dir / "libfaad.so.2"
                soname.unlink()
                soname.symlink_to(outside)
            return await super().run(argv, timeout=timeout, stdin=stdin, env=env)

    session = _EscapingRebuildSession(
        tc,
        source_dir=source_dir,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("rebuilt")),
        ],
    )

    result = await _executor(session).execute(
        "secb_patch_validation", "[Patch-Validator] validate", cve, {"root_id": uuid4()}
    )

    assert result.success is False
    assert "post-patch binary validation failed" in result.summary
    assert "resolved target escapes" in result.summary
    assert session.marker_invocations == []


async def test_patch_validation_accepts_declared_nonzero_normal_exit(tmp_path: Path) -> None:
    cve = _cve(exit_code=1)
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc, cve=cve)
    expected_exit = CommandOutcome(exit_code=1, output=_NO_CRASH)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            *_repro(expected_exit),
        ],
    )

    result = await _executor(session).execute(
        "secb_patch_validation", "[Patch-Validator]", cve, {"root_id": uuid4()}
    )

    assert result.success is True
    assert "REPRO_RUNS_NO_CRASH: 3/3" in (
        tc / "patch_validation_results.txt"
    ).read_text(encoding="utf-8")
    for index in (1, 2, 3):
        assert (tc / f"fix_run_{index}.log").read_text().rstrip().endswith("exit=1")


async def test_patch_validation_accepts_mixed_clean_declared_exits(tmp_path: Path) -> None:
    cve = _cve(exit_code=1)
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc, cve=cve)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            *_binary_validation(),
            *_direct_replay(CommandOutcome(exit_code=0, output=_NO_CRASH)),
            *_direct_replay(CommandOutcome(exit_code=1, output=_NO_CRASH)),
            *_direct_replay(CommandOutcome(exit_code=0, output=_NO_CRASH)),
        ],
    )

    result = await _executor(session).execute(
        "secb_patch_validation", "[Patch-Validator]", cve, {"root_id": uuid4()}
    )

    assert result.success is True
    assert "REPRO_RUNS_NO_CRASH: 3/3" in (
        tc / "patch_validation_results.txt"
    ).read_text(encoding="utf-8")
    assert [
        (tc / f"fix_run_{index}.log").read_text().rstrip().splitlines()[-1]
        for index in (1, 2, 3)
    ] == ["exit=0", "exit=1", "exit=0"]


async def test_patch_validation_dirty_apply_fails_and_skips_build(tmp_path: Path) -> None:
    # Given: secb patch reports a conflict
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = FakeSession(
        tc, responses=[_Resp("secb patch", _crash("error: patch failed: conflict in foo.c"))]
    )
    ex = _executor(session)

    # When
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: FAIL, and neither build nor direct replay is attempted
    assert result.success is False
    assert not any("secb build" in c for c in session.commands)
    assert not any(invocation[0][0] == "/work/bin/app" for invocation in session.invocations)
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "PATCH_APPLY_STATUS: conflicts" in verdict


async def test_patch_validation_build_failure_fails_and_skips_repro(tmp_path: Path) -> None:
    # Given: apply clean but the rebuild fails
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
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

    # Then: FAIL with BUILD_STATUS failed and no direct replay attempted
    assert result.success is False
    assert not any(invocation[0][0] == "/work/bin/app" for invocation in session.invocations)
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "BUILD_STATUS: failed" in verdict


async def test_patch_validation_recheck_restores_prepatch_state(tmp_path: Path) -> None:
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = StatefulPatchSession(tc)
    executor = _executor(session)
    root_id = uuid4()

    first = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 1},
    )
    second = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 2},
    )

    assert first.success is False
    assert second.success is True
    assert session.patch_calls == 2
    assert session.build_calls == 2
    assert (session.source_dir / "project" / "vulnerable.c").read_text(
        encoding="utf-8"
    ) == "void parse(void) { safe(); }\n"
    assert "REPRO_RUNS_NO_CRASH: 3/3" in (
        tc / "patch_validation_results.txt"
    ).read_text(encoding="utf-8")


async def test_patch_validation_resumed_initial_attempt_restores_state(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = StatefulPatchSession(tc)
    executor = _executor(session)
    root_id = uuid4()

    first = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 1},
    )
    resumed = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {
            "root_id": root_id,
            "procedure_attempt": 1,
            "procedure_resume": True,
        },
    )

    assert first.success is False
    assert resumed.success is True
    assert session.prepatch_observations == [
        b"void parse(void) { unsafe(); }\n",
        b"void parse(void) { unsafe(); }\n",
    ]


async def test_patch_validation_recheck_rejects_unapproved_source_drift(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = StatefulPatchSession(tc)
    executor = _executor(session)
    root_id = uuid4()

    first = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 1},
    )
    target = session.source_dir / "project" / "vulnerable.c"
    target.write_text("void parse(void) { unrelated(); }\n", encoding="utf-8")
    second = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 2},
    )

    assert first.success is False
    assert second.success is False
    assert "drifted outside" in second.summary
    assert session.patch_calls == 1
    assert session.build_calls == 1


async def test_patch_validation_recheck_restores_crlf_preimage_exactly(
    tmp_path: Path,
) -> None:
    original = b"// Cafe\xcc\x81\r\nvoid parse(void) { unsafe(); }\r\n"
    patched = b"// Cafe\xcc\x81\r\nvoid parse(void) { safe(); }\r\n"
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc, target_bytes=original)
    session = StatefulPatchSession(
        tc,
        original_source=original,
        patched_source=patched,
    )
    executor = _executor(session)
    root_id = uuid4()

    first = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 1},
    )
    second = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 2},
    )

    assert first.success is False
    assert second.success is True
    assert session.prepatch_observations == [original, original]
    assert (session.source_dir / "project" / "vulnerable.c").read_bytes() == patched


async def test_patch_validation_recheck_rejects_worker_planted_binary(
    tmp_path: Path,
) -> None:
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _crash("first build failed")),
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("no-op build")),
        ],
    )
    executor = _executor(session)
    root_id = uuid4()

    first = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 1},
    )
    _seed_elf(session.work_dir / "bin" / "app", payload=b"worker-planted-clean-fake")
    second = await executor.execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        _cve(),
        {"root_id": root_id, "procedure_attempt": 2},
    )

    assert first.success is False
    assert second.success is False
    assert "post-patch binary validation failed" in second.summary
    assert not (session.work_dir / "bin" / "app").exists()
    assert not any(
        invocation[0][0] == "/work/bin/app" for invocation in session.invocations
    )


async def test_patch_validation_recheck_cleans_untracked_git_build_inputs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "src"
    worktree = source_dir / "project"
    worktree.mkdir(parents=True)
    _git(worktree, "init")
    target = worktree / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    _git(worktree, "add", "vulnerable.c")
    _git(
        worktree,
        "-c",
        "user.name=Procedure Tests",
        "-c",
        "user.email=procedure-tests@example.invalid",
        "commit",
        "-m",
        "Seed fixture",
    )
    base_commit = _git(worktree, "rev-parse", "HEAD")
    cve = _cve().model_copy(
        update={"base_commit": base_commit, "work_dir": "/src/project"}
    )
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(tc, cve=cve, source_dir=source_dir)
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )
    session = StatefulPatchSession(tc)
    approval = await _executor(session).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )
    assert approval.success is True
    root_id = uuid4()

    first = await _executor(session).execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        cve,
        {"root_id": root_id, "procedure_attempt": 1},
    )
    planted = worktree / "attacker-controlled.o"
    planted.write_bytes(b"worker controlled build input")
    second = await _executor(session).execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        cve,
        {"root_id": root_id, "procedure_attempt": 2},
    )

    assert first.success is False
    assert second.success is True
    assert not planted.exists()


async def test_patch_validation_recheck_accepts_cleaned_in_tree_output_parent(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "src"
    worktree = source_dir / "project"
    worktree.mkdir(parents=True)
    _git(worktree, "init")
    target = worktree / "vulnerable.c"
    target.write_text("void parse(void) { unsafe(); }\n", encoding="utf-8")
    _git(worktree, "add", "vulnerable.c")
    _git(
        worktree,
        "-c",
        "user.name=Procedure Tests",
        "-c",
        "user.email=procedure-tests@example.invalid",
        "commit",
        "-m",
        "Seed fixture",
    )
    base_commit = _git(worktree, "rev-parse", "HEAD")
    cve = _cve().model_copy(
        update={"base_commit": base_commit, "work_dir": "/src/project"}
    )
    binary_path = "/src/project/frontend/.libs/app"
    tc = tmp_path / "testcase"
    await _freeze_exploit_identity(
        tc,
        cve=cve,
        source_dir=source_dir,
        binary_path=binary_path,
    )
    (tc / "root_cause_analysis.txt").write_text("verified cause\n", encoding="utf-8")
    (tc / "patch_plan.json").write_text(
        json.dumps(_patch_plan(target)), encoding="utf-8"
    )
    session = StatefulPatchSession(tc, binary_path=binary_path)
    approval = await _executor(session).execute(
        "secb_patch_apply", "[Patch-Applier] apply", cve, {"root_id": uuid4()}
    )
    assert approval.success is True
    root_id = uuid4()

    first = await _executor(session).execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        cve,
        {"root_id": root_id, "procedure_attempt": 1},
    )
    second = await _executor(session).execute(
        "secb_patch_validation",
        "[Patch-Validator] validate",
        cve,
        {"root_id": root_id, "procedure_attempt": 2},
    )

    assert first.success is False
    assert second.success is True
    assert _workspace_path(source_dir, session.work_dir, binary_path).exists()


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
    await _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            *_binary_validation(),
            *_direct_replay(_crash(_ASAN_HEAP)),
            *_direct_replay(_clean(_NO_CRASH), times=2),
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


@pytest.mark.parametrize(
    ("bad_outcome", "expected_reason"),
    [
        (
            CommandOutcome(
                exit_code=124,
                output=_ASAN_HEAP,
                termination=CommandTermination.TIMED_OUT,
            ),
            "timed out",
        ),
        (CommandOutcome(exit_code=7, output="repro error"), "exited nonzero (7)"),
        (CommandOutcome(exit_code=-11, output=""), "terminated by signal 11"),
        (CommandOutcome(exit_code=0, output="Assertion `size > 0' failed."), "assertion"),
        (
            CommandOutcome(exit_code=0, output="Segmentation fault (core dumped)"),
            "unsanitized crash",
        ),
    ],
    ids=["timeout", "nonzero", "signal", "assertion", "unsanitized-crash"],
)
async def test_patch_validation_rejects_every_failed_post_patch_outcome(
    tmp_path: Path,
    bad_outcome: CommandOutcome,
    expected_reason: str,
) -> None:
    # Given: apply and build succeed, but one post-patch replay has a host-observed failure
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp("secb build", _clean("built ok")),
            *_binary_validation(),
            *_direct_replay(bad_outcome),
            *_direct_replay(_clean(_NO_CRASH), times=2),
        ],
    )
    ex = _executor(session)

    # When: Patch-Validator evaluates the post-patch replay trio
    result = await ex.execute(
        "secb_patch_validation", "[Patch-Validator]", _cve(), {"root_id": uuid4()}
    )

    # Then: no failing outcome can be counted as a clean replay
    assert result.success is False
    assert expected_reason in result.summary
    verdict = (tc / "patch_validation_results.txt").read_text()
    assert "VERDICT: FAIL" in verdict
    assert "REPRO_RUNS_NO_CRASH: 3/3" not in verdict
    assert [invocation[0] for invocation in session.marker_invocations] == [
        ("/work/bin/app", "--help")
    ]


async def test_patch_validation_build_timeout_returns_failed_with_digest(tmp_path: Path) -> None:
    # Given: apply clean but the build times out with partial output
    tc = tmp_path / "testcase"
    await _seed_patch_inputs(tc)
    session = FakeSession(
        tc,
        responses=[
            _Resp("secb patch", _clean("applied")),
            _Resp(
                "secb build",
                CommandOutcome(
                    exit_code=124,
                    output="partial build log tail",
                    termination=CommandTermination.TIMED_OUT,
                ),
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
    await _seed_patch_inputs(tc)
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
